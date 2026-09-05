"""Calendar bot: Telegram message -> LLM -> Google Calendar event.

Entry point for a 2nd-gen Google Cloud Function (HTTP trigger). Telegram POSTs
every message to this function; we parse it with an OpenAI-compatible LLM and
either insert the resulting event into Google Calendar or change an event
that's already there, then reply with a link.

Everything is configured through environment variables (see README.md).
"""

from __future__ import annotations

import datetime as dt
import hmac
import html
import json
import logging
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

import functions_framework
import google.auth
import requests
from google.auth import impersonated_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build as build_google_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("calendar-bot")

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
TELEGRAM_API = "https://api.telegram.org"
METADATA_EMAIL_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/email"
)

HELP_TEXT = (
    "Send me an event in plain English and I'll put it on your calendar.\n\n"
    "Examples:\n"
    "• <code>dentist next Tuesday 3pm</code>\n"
    "• <code>lunch with Sam Thursday 12:30 at Zuni</code>\n"
    "• <code>flight to Denver Oct 4, all day</code>\n"
    "• <code>standup tomorrow 9:15am for 15 minutes</code>\n"
    "• <code>book club every Saturday 7pm</code>\n"
    "• <code>gym Mon Wed Fri 6am for 8 weeks</code>\n\n"
    "Already on the calendar? Say what should change and I'll find it:\n"
    "• <code>move the dentist to 4pm</code>\n"
    "• <code>push standup back 15 minutes</code>\n"
    "• <code>lunch with Sam is at Zuni now</code>\n"
    "• <code>make book club an hour and a half</code>\n"
    "• <code>rename book club to reading group</code>"
)

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
# A weekday code, optionally prefixed with an ordinal: SA, 1MO, -1FR.
BYDAY_RE = re.compile(r"^(-?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")

# How far to look for an event the user wants to change, when the message
# doesn't say. Wide by default because the search is keyword-driven; narrow
# when there's no keyword to search on and we have to list everything.
SEARCH_PAST_DAYS = 30
SEARCH_FUTURE_DAYS = 365
BROWSE_PAST_DAYS = 7
BROWSE_FUTURE_DAYS = 60
MAX_CANDIDATES = 25


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _config() -> dict[str, Any]:
    return {
        "bot_token": _env("TELEGRAM_BOT_TOKEN", required=True),
        "webhook_secret": _env("TELEGRAM_WEBHOOK_SECRET", required=True),
        "allowed_user_id": _env("ALLOWED_TELEGRAM_USER_ID", required=True),
        "llm_base_url": _env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "llm_model": _env("LLM_MODEL", "deepseek-v4-flash"),
        "llm_api_key": _env("LLM_API_KEY", required=True),
        "llm_timeout": int(_env("LLM_TIMEOUT_SECONDS", "25")),
        "calendar_id": _env("CALENDAR_ID", required=True),
        "timezone": _env("TIMEZONE", "America/New_York"),
        "default_minutes": int(_env("DEFAULT_EVENT_MINUTES", "60")),
    }


# --------------------------------------------------------------------------
# Google Calendar
# --------------------------------------------------------------------------

_calendar_service = None  # cached across warm invocations


def _runtime_service_account_email() -> str:
    """The service account this function runs as."""
    email = os.environ.get("SERVICE_ACCOUNT_EMAIL")
    if email:
        return email
    resp = requests.get(
        METADATA_EMAIL_URL, headers={"Metadata-Flavor": "Google"}, timeout=5
    )
    resp.raise_for_status()
    return resp.text.strip()


def _calendar_credentials():
    """Credentials with the Calendar scope.

    Two supported modes:

    * Keyless (default). We hold a metadata-server token for our own service
      account, which only carries the cloud-platform scope -- and that scope
      does not cover Calendar, a Workspace API. So we self-impersonate through
      the IAM Credentials API to mint a token that *does* carry the Calendar
      scope. Requires roles/iam.serviceAccountTokenCreator on ourselves.
    * A service account JSON key in GOOGLE_SA_KEY_JSON, if you'd rather manage
      a key than the self-impersonation grant.
    """
    raw_key = os.environ.get("GOOGLE_SA_KEY_JSON", "").strip()
    if raw_key:
        info = json.loads(raw_key)
        return service_account.Credentials.from_service_account_info(
            info, scopes=CALENDAR_SCOPES
        )

    source, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=_runtime_service_account_email(),
        target_scopes=CALENDAR_SCOPES,
        lifetime=3600,
    )


def calendar_service():
    global _calendar_service
    if _calendar_service is None:
        _calendar_service = build_google_client(
            "calendar",
            "v3",
            credentials=_calendar_credentials(),
            cache_discovery=False,
        )
    return _calendar_service


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def send_message(bot_token: str, chat_id: int | str, text: str) -> None:
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("sendMessage failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException:
        log.exception("sendMessage request failed")


def send_typing(bot_token: str, chat_id: int | str) -> None:
    """Best-effort 'typing…' indicator while the LLM thinks."""
    try:
        requests.post(
            f"{TELEGRAM_API}/bot{bot_token}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------
# LLM parsing
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You turn one short natural-language message into an \
instruction for the user's calendar. The message either describes a new event \
or asks to change an event that is already on the calendar.

Reply with ONE JSON object and nothing else. Schema:
{{
  "intent": "create" or "update" or "other",
  "error": string or null,
  "search": object or null,
  "title": string,
  "all_day": boolean,
  "start": string,
  "end": string,
  "location": string or null,
  "description": string or null,
  "recurrence": object or null
}}

Choose "intent" first:
- "create" -- the message describes a new event. Fill in the event fields and \
set "search" to null.
- "update" -- the message points at an event that already exists and asks to \
change it: move it, reschedule it, make it longer or shorter, rename it, \
change where it is. Fill in "search" and set every event field to null. Do not \
work out the new values here; you will be shown the matching event afterwards.
- "other" -- anything else. Set "error" to a one-sentence explanation and \
every other field to null or false.

Treat the message as an update whenever it talks about the event as something \
that exists ("move", "reschedule", "push back", "change", "rename", "actually \
it's at ...") rather than as something to add.

"search" says how to find that existing event:
  {{"query": string, "window_start": "YYYY-MM-DD" or null, \
"window_end": "YYYY-MM-DD" or null}}
- "query" is one to three words likely to appear in the event's title on the \
calendar. Leave out dates, times, and every word about the change itself: \
"move my dentist appointment to 4pm" -> "dentist"; "push tomorrow's standup \
back 15 min" -> "standup". Use "" only if the message names nothing \
distinctive.
- The window bounds the days worth searching. Leave both null for "any time"; \
set them only when the message points at a period ("last week's ...", "the \
lunch on the 12th").

The remaining fields describe a new event, for "create":
- "start" and "end" are local wall-clock times in the user's timezone. Never \
include a UTC offset or timezone name.
  - Timed event: "YYYY-MM-DDTHH:MM:SS" using a 24-hour clock.
  - All-day event: "YYYY-MM-DD", where "end" is the LAST day of the event, \
inclusive (same as "start" for a one-day event).
- Resolve relative dates ("tomorrow", "next Tuesday", "in 3 weeks") against the \
current time given by the user. Always choose the next future occurrence.
- If the message gives no time of day, make it an all-day event.
- If the message gives no end time or duration, use {default_minutes} minutes.
- "title" is short and specific. Do not put the date or time in the title.
- "location" only if the message actually names a place; otherwise null.
- "description" only for detail that doesn't fit the title; otherwise null.
- "recurrence" is null unless the message clearly describes something \
repeating ("every", "each", "weekly", "daily", "on Mondays"). For a repeating \
event it is an object:
  {{"freq": "DAILY"|"WEEKLY"|"MONTHLY"|"YEARLY", "interval": integer >= 1, \
"byday": array of strings or null, "count": integer or null, \
"until": "YYYY-MM-DD" or null}}
  - "byday" holds weekday codes MO TU WE TH FR SA SU. Use it for WEEKLY events: \
"every Tuesday and Thursday" -> ["TU","TH"], "weekdays" -> \
["MO","TU","WE","TH","FR"]. For MONTHLY you may prefix an ordinal: \
"first Monday" -> ["1MO"], "last Friday" -> ["-1FR"]. Otherwise null.
  - "interval" is 1 unless the message says otherwise: "every other week" -> 2, \
"every 3 days" -> 3.
  - Set "count" for a fixed number of occurrences ("for 6 weeks" -> 6). Set \
"until" for an end date ("until December 20"). Never set both. Both null means \
it repeats forever, which is the normal case.
  - "start" and "end" describe the FIRST occurrence and must fall on a day the \
pattern allows."""


SELECT_PROMPT = """The user wants to change an event that is already on their \
calendar. You get their message and a numbered list of candidate events. Work \
out which one they mean and what should change about it.

Reply with ONE JSON object and nothing else. Schema:
{
  "error": string or null,
  "match": integer or null,
  "scope": "this" or "all",
  "changes": {
    "title": string or null,
    "all_day": boolean or null,
    "start": string or null,
    "end": string or null,
    "location": string or null,
    "description": string or null
  }
}

Rules:
- "match" is the "index" of the candidate the message is about. The titles will \
rarely match the message word for word, so pick the single best fit. Only when \
no candidate is plausible, set "error" to a one-sentence explanation and leave \
"match" null.
- If several candidates fit, prefer the soonest one that is still in the \
future, unless the message points somewhere else.
- Every field of "changes" stays null unless the message asks to change it. \
Never restate a value that isn't changing.
- A new "title" is short and specific, capitalised the way a calendar title \
is, whatever case the message used. Never put the date or time in it.
- "start" and "end" are the NEW local wall-clock values, never a UTC offset or \
a delta: "YYYY-MM-DDTHH:MM:SS" for a timed event, "YYYY-MM-DD" for an all-day \
one, where "end" is the LAST day, inclusive. "push it back an hour" on a 3pm \
event gives "start" of 16:00 the same day.
- Moving an event means setting "start" only; leave "end" null and it keeps its \
current length. Set "end" when the message changes the length or the end time \
("make it 2 hours", "have it run until 5").
- Use "" for "location" or "description" to clear one.
- "scope" is "this" for one occurrence and "all" for every occurrence of a \
repeating event. Candidates with "repeats": true are one occurrence of a \
series. Default to "this"; use "all" only when the message clearly means the \
whole series ("every", "from now on", "all my ..."). Always "this" for a \
candidate that does not repeat.
- Set "error" if the message asks for something these fields can't express -- \
deleting the event, or changing how often it repeats."""


def build_user_prompt(text: str, now: dt.datetime, tz_name: str) -> str:
    return (
        f"Current time: {now.strftime('%Y-%m-%dT%H:%M:%S')} "
        f"({now.strftime('%A')}), timezone {tz_name} "
        f"(UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]}).\n"
        f"Message: {text}"
    )


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _llm_json(
    system: str, user: str, cfg: dict[str, Any]
) -> dict[str, Any]:
    """One strict-JSON round trip to the LLM. Raises RuntimeError on failure."""
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            f"{cfg['llm_base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['llm_api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=cfg["llm_timeout"],
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the LLM: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"LLM returned {resp.status_code}: {resp.text[:300]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {resp.text[:300]}") from exc

    try:
        parsed = json.loads(_strip_code_fences(content))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM did not return JSON: {content[:300]}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM did not return a JSON object: {content[:300]}")
    return parsed


def parse_event(text: str, cfg: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Classify one message and, for a new event, describe it."""
    return _llm_json(
        SYSTEM_PROMPT.format(default_minutes=cfg["default_minutes"]),
        build_user_prompt(text, now, cfg["timezone"]),
        cfg,
    )


def choose_update(
    text: str,
    candidates: list[dict[str, Any]],
    cfg: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any]:
    """Ask the LLM which candidate the message means, and what to change."""
    tz = ZoneInfo(cfg["timezone"])
    views = [candidate_view(i, event, tz) for i, event in enumerate(candidates)]
    user = (
        build_user_prompt(text, now, cfg["timezone"])
        + "\nCandidates:\n"
        + json.dumps(views, indent=None)
    )
    return _llm_json(SELECT_PROMPT, user, cfg)


# --------------------------------------------------------------------------
# Event construction
# --------------------------------------------------------------------------


def _parse_local_datetime(value: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _normalize_byday(value: Any) -> list[str]:
    """Validate the LLM's weekday list into RFC 5545 BYDAY tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise RuntimeError(f"Unreadable repeat days: {value!r}")

    days: list[str] = []
    for item in value:
        token = str(item).strip().upper()
        if not BYDAY_RE.match(token):
            raise RuntimeError(f"Unsupported repeat day: {item!r}")
        if token not in days:
            days.append(token)
    return days


def _positive_int(value: Any, field: str, maximum: int) -> int | None:
    # Not `value in (None, "", False)`: 0 == False, and 0 is a value we must
    # reject loudly rather than treat as absent.
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"Unreadable repeat {field}: {value!r}") from None
    if not 1 <= number <= maximum:
        raise RuntimeError(f"Repeat {field} out of range: {number}")
    return number


def _until_token(until_raw: str, all_day: bool, tz: ZoneInfo) -> str:
    """RFC 5545 requires UNTIL to match DTSTART's type: a bare date for all-day
    events, and a UTC date-time for timed ones."""
    try:
        until_date = dt.date.fromisoformat(str(until_raw)[:10])
    except ValueError:
        raise RuntimeError(f"Unreadable repeat end date: {until_raw!r}") from None

    if all_day:
        return until_date.strftime("%Y%m%d")
    # Through the end of that day, in the user's zone, expressed as UTC.
    last_moment = dt.datetime.combine(until_date, dt.time(23, 59, 59), tzinfo=tz)
    return last_moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_recurrence_rule(
    spec: dict[str, Any], all_day: bool, tz: ZoneInfo
) -> tuple[str, list[str]]:
    """Turn the LLM's recurrence object into an RRULE string.

    Returns the rule plus the weekdays the first occurrence is allowed to fall
    on -- empty unless this is a plain weekly pattern, which is the only case
    the caller can safely realign.
    """
    freq = str(spec.get("freq") or "").strip().upper()
    if freq not in FREQUENCIES:
        raise RuntimeError(f"Unsupported repeat frequency: {spec.get('freq')!r}")

    byday = _normalize_byday(spec.get("byday"))
    if byday and freq in ("DAILY", "YEARLY"):
        # BYDAY is meaningful for these in RFC 5545 but almost never what was
        # meant; dropping it beats emitting a rule the user didn't ask for.
        log.warning("Ignoring byday=%s on a %s rule", byday, freq)
        byday = []

    parts = [f"FREQ={freq}"]

    interval = _positive_int(spec.get("interval"), "interval", 999) or 1
    if interval > 1:
        parts.append(f"INTERVAL={interval}")

    if byday:
        parts.append("BYDAY=" + ",".join(byday))

    count = _positive_int(spec.get("count"), "count", 730)
    until = spec.get("until")
    if count:
        parts.append(f"COUNT={count}")
    elif until:
        parts.append(f"UNTIL={_until_token(until, all_day, tz)}")

    snap_days = byday if freq == "WEEKLY" and all(len(d) == 2 for d in byday) else []
    return "RRULE:" + ";".join(parts), snap_days


def _days_to_first_match(weekday: int, byday: list[str]) -> int:
    """How far the start has to slide to land on one of the BYDAY weekdays.

    An RRULE never suppresses DTSTART, so a first occurrence that doesn't match
    the pattern shows up as one stray event before the series settles in.
    """
    wanted = {WEEKDAYS.index(token[-2:]) for token in byday}
    return min((want - weekday) % 7 for want in wanted)


def build_event_body(parsed: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Turn the LLM's JSON into a Google Calendar event resource."""
    tz = ZoneInfo(cfg["timezone"])

    title = (parsed.get("title") or "").strip()
    if not title:
        raise RuntimeError("The LLM did not give the event a title.")

    start_raw = parsed.get("start")
    if not isinstance(start_raw, str) or not start_raw:
        raise RuntimeError("The LLM did not give the event a start time.")
    end_raw = parsed.get("end") if isinstance(parsed.get("end"), str) else None

    all_day = bool(parsed.get("all_day"))

    recurrence_spec = parsed.get("recurrence")
    rule, snap_days = (
        build_recurrence_rule(recurrence_spec, all_day, tz)
        if isinstance(recurrence_spec, dict) and recurrence_spec.get("freq")
        else (None, [])
    )

    try:
        if all_day:
            start_date = dt.date.fromisoformat(start_raw[:10])
            end_date = dt.date.fromisoformat((end_raw or start_raw)[:10])
            if end_date < start_date:
                end_date = start_date
            if snap_days:
                shift = dt.timedelta(
                    days=_days_to_first_match(start_date.weekday(), snap_days)
                )
                start_date, end_date = start_date + shift, end_date + shift
            # Google's all-day end date is exclusive; the LLM gives us the
            # last day inclusive, so add one.
            start_field = {"date": start_date.isoformat()}
            end_field = {"date": (end_date + dt.timedelta(days=1)).isoformat()}
        else:
            start_dt = _parse_local_datetime(start_raw, tz)
            end_dt = _parse_local_datetime(end_raw, tz) if end_raw else None
            if end_dt is None or end_dt <= start_dt:
                end_dt = start_dt + dt.timedelta(minutes=cfg["default_minutes"])
            if snap_days:
                shift = dt.timedelta(
                    days=_days_to_first_match(start_dt.weekday(), snap_days)
                )
                # Shift the wall-clock date, not the instant, so a series that
                # crosses a DST boundary keeps its 7pm.
                start_dt = (start_dt.replace(tzinfo=None) + shift).replace(tzinfo=tz)
                end_dt = (end_dt.replace(tzinfo=None) + shift).replace(tzinfo=tz)
            start_field = {"dateTime": start_dt.isoformat(), "timeZone": cfg["timezone"]}
            end_field = {"dateTime": end_dt.isoformat(), "timeZone": cfg["timezone"]}
    except ValueError as exc:
        raise RuntimeError(f"The LLM returned an unreadable date: {exc}") from exc

    body: dict[str, Any] = {"summary": title, "start": start_field, "end": end_field}
    if rule:
        body["recurrence"] = [rule]

    location = parsed.get("location")
    if isinstance(location, str) and location.strip():
        body["location"] = location.strip()

    description = parsed.get("description")
    if isinstance(description, str) and description.strip():
        body["description"] = description.strip()

    return body


# --------------------------------------------------------------------------
# Finding and changing an existing event
# --------------------------------------------------------------------------


def _naive(value: dt.datetime) -> dt.datetime:
    """Wall-clock, zone dropped. Arithmetic on these keeps 3pm at 3pm across a
    DST boundary, which arithmetic on the instant would not."""
    return value.replace(tzinfo=None)


def _event_bounds(
    event: dict[str, Any], tz: ZoneInfo
) -> tuple[bool, Any, Any]:
    """(all_day, start, end) in local terms.

    Dates for an all-day event, where end is the LAST day inclusive -- the
    opposite of Google's exclusive end, and the same convention the LLM uses.
    Aware datetimes otherwise.
    """
    start, end = event.get("start") or {}, event.get("end") or {}
    if "date" in start:
        start_date = dt.date.fromisoformat(start["date"])
        last_day = dt.date.fromisoformat(
            end.get("date") or start["date"]
        ) - dt.timedelta(days=1)
        return True, start_date, max(last_day, start_date)
    start_dt = _parse_local_datetime(start["dateTime"], tz)
    end_dt = _parse_local_datetime(end.get("dateTime") or start["dateTime"], tz)
    return False, start_dt, end_dt


def candidate_view(index: int, event: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    """One event boiled down to what the LLM needs to recognise it."""
    all_day, start, end = _event_bounds(event, tz)
    view: dict[str, Any] = {
        "index": index,
        "title": event.get("summary") or "(untitled)",
        "day": start.strftime("%A"),
        "all_day": all_day,
        "start": start.strftime("%Y-%m-%dT%H:%M:%S") if not all_day else start.isoformat(),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S") if not all_day else end.isoformat(),
        "repeats": bool(event.get("recurringEventId")),
    }
    for field in ("location", "description"):
        value = event.get(field)
        if value:
            view[field] = str(value)[:200]
    return view


def _window_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        log.warning("Ignoring unreadable search window bound %r", value)
        return None


def find_candidates(
    search: dict[str, Any], cfg: dict[str, Any], now: dt.datetime
) -> list[dict[str, Any]]:
    """Events the message might be talking about, soonest first.

    Google's `q` does the first cut. If it finds nothing -- a typo, or a title
    that shares no words with the message -- we fall back to listing a tight
    window around today and let the LLM read the titles itself.
    """
    tz = ZoneInfo(cfg["timezone"])
    today = now.date()
    given_start = _window_date(search.get("window_start"))
    given_end = _window_date(search.get("window_end"))

    def window(past_days: int, future_days: int) -> dict[str, str]:
        start = given_start or today - dt.timedelta(days=past_days)
        end = given_end or today + dt.timedelta(days=future_days)
        if end < start:
            start, end = end, start
        return {
            "timeMin": dt.datetime.combine(start, dt.time.min, tzinfo=tz).isoformat(),
            "timeMax": dt.datetime.combine(
                end + dt.timedelta(days=1), dt.time.min, tzinfo=tz
            ).isoformat(),
        }

    def listing(bounds: dict[str, str], query: str | None) -> list[dict[str, Any]]:
        params = {
            "calendarId": cfg["calendar_id"],
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": MAX_CANDIDATES,
            **bounds,
        }
        if query:
            params["q"] = query
        items = calendar_service().events().list(**params).execute().get("items", [])
        return [
            item
            for item in items
            if item.get("status") != "cancelled" and item.get("start")
        ]

    query = str(search.get("query") or "").strip()
    if query:
        found = listing(window(SEARCH_PAST_DAYS, SEARCH_FUTURE_DAYS), query)
        if found:
            return found
    return listing(window(BROWSE_PAST_DAYS, BROWSE_FUTURE_DAYS), None)


def _changed_text(changes: dict[str, Any], field: str) -> str | None:
    value = changes.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_update_patch(
    event: dict[str, Any], changes: dict[str, Any], cfg: dict[str, Any]
) -> tuple[dict[str, Any], tuple[dt.timedelta, dt.timedelta] | None]:
    """Turn the LLM's change set into a Calendar patch for one event.

    Also returns how far start and end moved, which is what the caller needs to
    push the same move onto a whole series instead of one occurrence. It's None
    when the move can't be expressed as a shift -- an event switching between
    timed and all-day.
    """
    tz = ZoneInfo(cfg["timezone"])
    patch: dict[str, Any] = {}

    title = _changed_text(changes, "title")
    if title:
        patch["summary"] = title
    for field in ("location", "description"):
        value = changes.get(field)
        if isinstance(value, str):
            patch[field] = value.strip()  # "" clears it

    start_raw = _changed_text(changes, "start")
    end_raw = _changed_text(changes, "end")
    wanted_all_day = changes.get("all_day")
    was_all_day, cur_start, cur_end = _event_bounds(event, tz)
    all_day = wanted_all_day if isinstance(wanted_all_day, bool) else was_all_day

    if not (start_raw or end_raw or all_day != was_all_day):
        return patch, None

    try:
        if all_day:
            cur_start_date = cur_start if was_all_day else cur_start.date()
            cur_end_date = cur_end if was_all_day else cur_end.date()
            new_start = (
                dt.date.fromisoformat(start_raw[:10]) if start_raw else cur_start_date
            )
            if end_raw:
                new_end = dt.date.fromisoformat(end_raw[:10])
            else:
                new_end = cur_end_date + (new_start - cur_start_date)
            new_end = max(new_end, new_start)
            patch["start"] = {"date": new_start.isoformat()}
            # Google's all-day end is exclusive; ours is the last day.
            patch["end"] = {"date": (new_end + dt.timedelta(days=1)).isoformat()}
            shift = (
                (new_start - cur_start_date, new_end - cur_end_date)
                if was_all_day
                else None
            )
        else:
            if start_raw:
                new_start = _parse_local_datetime(start_raw, tz)
            elif was_all_day:
                raise RuntimeError("Tell me what time it should start.")
            else:
                new_start = cur_start

            if end_raw:
                new_end = _parse_local_datetime(end_raw, tz)
            elif was_all_day:
                new_end = new_start + dt.timedelta(minutes=cfg["default_minutes"])
            else:
                # No new end: keep the length the event already had.
                moved = _naive(new_start) - _naive(cur_start)
                new_end = (_naive(cur_end) + moved).replace(tzinfo=tz)
            if new_end <= new_start:
                new_end = new_start + dt.timedelta(minutes=cfg["default_minutes"])

            patch["start"] = {"dateTime": new_start.isoformat(), "timeZone": cfg["timezone"]}
            patch["end"] = {"dateTime": new_end.isoformat(), "timeZone": cfg["timezone"]}
            shift = (
                None
                if was_all_day
                else (
                    _naive(new_start) - _naive(cur_start),
                    _naive(new_end) - _naive(cur_end),
                )
            )
    except ValueError as exc:
        raise RuntimeError(f"The LLM returned an unreadable date: {exc}") from exc

    return patch, shift


def shift_event_times(
    event: dict[str, Any], shift: tuple[dt.timedelta, dt.timedelta], cfg: dict[str, Any]
) -> dict[str, Any]:
    """The start/end half of a patch that moves `event` by `shift`.

    The LLM works from one occurrence, so its new times are absolute and only
    fit that occurrence. Re-deriving them from the series' own start is what
    makes "move all my standups to 4pm" land on the series.
    """
    tz = ZoneInfo(cfg["timezone"])
    start_delta, end_delta = shift
    start, end = event.get("start") or {}, event.get("end") or {}

    if "date" in start:
        new_start = dt.date.fromisoformat(start["date"]) + start_delta
        new_end = dt.date.fromisoformat(end["date"]) + end_delta
        return {"start": {"date": new_start.isoformat()}, "end": {"date": new_end.isoformat()}}

    new_start = (
        _naive(_parse_local_datetime(start["dateTime"], tz)) + start_delta
    ).replace(tzinfo=tz)
    new_end = (_naive(_parse_local_datetime(end["dateTime"], tz)) + end_delta).replace(
        tzinfo=tz
    )
    return {
        "start": {"dateTime": new_start.isoformat(), "timeZone": cfg["timezone"]},
        "end": {"dateTime": new_end.isoformat(), "timeZone": cfg["timezone"]},
    }


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------

DAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}
ORDINAL_NAMES = {
    "1": "first",
    "2": "second",
    "3": "third",
    "4": "fourth",
    "5": "fifth",
    "-1": "last",
}
UNIT_NAMES = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}


def _join_days(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _describe_day(token: str) -> str:
    day = DAY_NAMES[token[-2:]]
    ordinal = token[:-2]
    return f"{ORDINAL_NAMES.get(ordinal, ordinal)} {day}" if ordinal else day


def describe_recurrence(rule: str) -> str:
    """Plain-English summary of an RRULE we generated, for the receipt."""
    parts = dict(
        piece.split("=", 1)
        for piece in rule.removeprefix("RRULE:").split(";")
        if "=" in piece
    )
    freq = parts.get("FREQ", "")
    if freq not in UNIT_NAMES:
        return rule

    interval = int(parts.get("INTERVAL", "1"))
    unit = UNIT_NAMES[freq]
    days = [_describe_day(d) for d in parts["BYDAY"].split(",")] if "BYDAY" in parts else []

    if freq == "WEEKLY" and days:
        text = (
            f"Every {_join_days(days)}"
            if interval == 1
            else f"Every {interval} weeks on {_join_days(days)}"
        )
    elif days:
        every = "Every month" if interval == 1 else f"Every {interval} months"
        text = f"{every} on the {_join_days(days)}"
    else:
        text = f"Every {unit}" if interval == 1 else f"Every {interval} {unit}s"

    if "COUNT" in parts:
        text += f", {parts['COUNT']} times"
    elif "UNTIL" in parts:
        stamp = parts["UNTIL"]
        try:
            last = dt.datetime.strptime(stamp[:8], "%Y%m%d").date()
            text += f", until {last.strftime('%b %-d, %Y')}"
        except ValueError:
            text += f", until {stamp}"
    return text


def describe_when(event: dict[str, Any], tz_name: str) -> str:
    """"Thu, Aug 20, 2026 · 3:00 PM – 4:00 PM", or the all-day equivalent."""
    all_day, start, end = _event_bounds(event, ZoneInfo(tz_name))
    if all_day:
        if end <= start:
            return start.strftime("%a, %b %-d, %Y") + " · all day"
        return (
            f"{start.strftime('%a, %b %-d')} – "
            f"{end.strftime('%a, %b %-d, %Y')} · all day"
        )
    return (
        f"{start.strftime('%a, %b %-d, %Y')} · "
        f"{start.strftime('%-I:%M %p')} – {end.strftime('%-I:%M %p')}"
    )


def format_confirmation(
    event: dict[str, Any],
    tz_name: str,
    *,
    icon: str = "✅",
    extra: list[str] | None = None,
) -> str:
    """Human-readable receipt for an event we just created or changed."""
    lines = [
        f"{icon} <b>{html.escape(event.get('summary', 'Event'))}</b>",
        f"🗓 {html.escape(describe_when(event, tz_name))}",
    ]
    lines.extend(extra or [])
    for rule in event.get("recurrence") or []:
        if str(rule).startswith("RRULE:"):
            lines.append(f"🔁 {html.escape(describe_recurrence(rule))}")
    if event.get("location"):
        lines.append(f"📍 {html.escape(event['location'])}")
    if event.get("htmlLink"):
        lines.append(f'<a href="{html.escape(event["htmlLink"])}">Open in Calendar</a>')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------


def _complain(chat_id: int | str, cfg: dict[str, Any], message: str) -> None:
    send_message(cfg["bot_token"], chat_id, f"⚠️ {html.escape(message)}")


def _calendar_failure(
    chat_id: int | str, cfg: dict[str, Any], what: str, exc: Exception
) -> None:
    send_message(
        cfg["bot_token"],
        chat_id,
        f"⚠️ Couldn't {what}:\n<code>{html.escape(str(exc)[:400])}</code>",
    )


def handle_create(
    chat_id: int | str, cfg: dict[str, Any], parsed: dict[str, Any]
) -> None:
    try:
        body = build_event_body(parsed, cfg)
    except RuntimeError as exc:
        log.error("Bad event from LLM: %s (%s)", exc, parsed)
        _complain(chat_id, cfg, str(exc))
        return

    try:
        event = (
            calendar_service()
            .events()
            .insert(calendarId=cfg["calendar_id"], body=body)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar insert failed")
        _calendar_failure(chat_id, cfg, "add the event to your calendar", exc)
        return

    send_message(cfg["bot_token"], chat_id, format_confirmation(event, cfg["timezone"]))


def handle_update(
    text: str,
    chat_id: int | str,
    cfg: dict[str, Any],
    parsed: dict[str, Any],
    now: dt.datetime,
) -> None:
    """Find the event the message is about, then patch it."""
    search = parsed.get("search")
    if not isinstance(search, dict):
        search = {}

    try:
        candidates = find_candidates(search, cfg, now)
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar search failed")
        _calendar_failure(chat_id, cfg, "search your calendar", exc)
        return

    if not candidates:
        send_message(
            cfg["bot_token"],
            chat_id,
            "🤔 I couldn't find an event like that on your calendar.",
        )
        return

    try:
        decision = choose_update(text, candidates, cfg, now)
    except RuntimeError as exc:
        log.exception("LLM match failed")
        _complain(chat_id, cfg, str(exc))
        return

    if decision.get("error"):
        send_message(
            cfg["bot_token"], chat_id, f"🤔 {html.escape(str(decision['error']))}"
        )
        return

    try:
        index = int(decision.get("match"))
        event = candidates[index]
    except (TypeError, ValueError, IndexError):
        log.error("LLM picked no usable candidate: %s", decision)
        send_message(
            cfg["bot_token"],
            chat_id,
            "🤔 I couldn't tell which event you meant. Try naming it the way "
            "it appears on your calendar.",
        )
        return

    changes = decision.get("changes")
    if not isinstance(changes, dict):
        changes = {}

    try:
        patch, shift = build_update_patch(event, changes, cfg)
    except RuntimeError as exc:
        log.error("Bad change set from LLM: %s (%s)", exc, decision)
        _complain(chat_id, cfg, str(exc))
        return

    if not patch:
        send_message(
            cfg["bot_token"],
            chat_id,
            f"🤔 I found <b>{html.escape(event.get('summary') or 'that event')}</b> "
            "but couldn't tell what to change about it.",
        )
        return

    series = str(decision.get("scope") or "this").lower() == "all"
    series = series and bool(event.get("recurringEventId"))
    target = event["id"]

    try:
        if series:
            target = event["recurringEventId"]
            master = (
                calendar_service()
                .events()
                .get(calendarId=cfg["calendar_id"], eventId=target)
                .execute()
            )
            if shift and ("start" in patch or "end" in patch):
                # The LLM's absolute times belong to the occurrence it was
                # shown; the series starts on some other day.
                patch.update(shift_event_times(master, shift, cfg))
            before = describe_when(master, cfg["timezone"])
        else:
            before = describe_when(event, cfg["timezone"])

        updated = (
            calendar_service()
            .events()
            .patch(calendarId=cfg["calendar_id"], eventId=target, body=patch)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar update failed")
        _calendar_failure(chat_id, cfg, "update that event", exc)
        return

    extra = []
    after = describe_when(updated, cfg["timezone"])
    if after != before:
        extra.append(f"↩️ <s>{html.escape(before)}</s>")
    if event.get("recurringEventId"):
        extra.append("🔁 every occurrence" if series else "🔂 this occurrence only")

    send_message(
        cfg["bot_token"],
        chat_id,
        format_confirmation(updated, cfg["timezone"], icon="✏️", extra=extra),
    )


def handle_text(text: str, chat_id: int | str, cfg: dict[str, Any]) -> None:
    """Parse one message and act on it. Never raises."""
    command = text.strip().split()[0].lower().split("@")[0] if text.strip() else ""
    if command in {"/start", "/help"}:
        send_message(cfg["bot_token"], chat_id, HELP_TEXT)
        return

    send_typing(cfg["bot_token"], chat_id)

    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))
    try:
        parsed = parse_event(text, cfg, now)
    except RuntimeError as exc:
        log.exception("LLM parse failed")
        _complain(chat_id, cfg, str(exc))
        return

    if parsed.get("error"):
        send_message(
            cfg["bot_token"], chat_id, f"🤔 {html.escape(str(parsed['error']))}"
        )
        return

    if str(parsed.get("intent") or "create").lower() == "update":
        handle_update(text, chat_id, cfg, parsed, now)
        return
    handle_create(chat_id, cfg, parsed)


@functions_framework.http
def telegram_webhook(request):
    """HTTP entry point. Always returns 200 to Telegram so it doesn't retry."""
    if request.method == "GET":
        return ("calendar-bot is up", 200)
    if request.method != "POST":
        return ("", 405)

    try:
        cfg = _config()
    except RuntimeError:
        log.exception("Bad configuration")
        return ("misconfigured", 500)

    presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(presented, cfg["webhook_secret"]):
        log.warning("Rejected request with a bad or missing webhook secret")
        return ("forbidden", 403)

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return ("ignored", 200)

    sender_id = str((message.get("from") or {}).get("id", ""))
    chat_id = (message.get("chat") or {}).get("id")
    if sender_id != str(cfg["allowed_user_id"]):
        log.warning("Ignoring message from unauthorized user id %r", sender_id)
        return ("ignored", 200)

    text = message.get("text")
    if not text:
        send_message(cfg["bot_token"], chat_id, "Send me a text message describing the event.")
        return ("ignored", 200)

    try:
        handle_text(text, chat_id, cfg)
    except Exception:  # noqa: BLE001 - a 500 makes Telegram retry; don't
        log.exception("Unhandled error")
        send_message(cfg["bot_token"], chat_id, "⚠️ Something went wrong. Check the logs.")

    return ("ok", 200)
