"""Calendar bot: Telegram message -> LLM -> Google Calendar event.

Entry point for a 2nd-gen Google Cloud Function (HTTP trigger). Telegram POSTs
every message to this function; we parse it with an OpenAI-compatible LLM,
insert the resulting event into Google Calendar, and reply with a link.

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
    "• <code>gym Mon Wed Fri 6am for 8 weeks</code>"
)

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
# A weekday code, optionally prefixed with an ordinal: SA, 1MO, -1FR.
BYDAY_RE = re.compile(r"^(-?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")


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

SYSTEM_PROMPT = """You convert one short natural-language message into a single \
calendar event.

Reply with ONE JSON object and nothing else. Schema:
{{
  "error": string or null,
  "title": string,
  "all_day": boolean,
  "start": string,
  "end": string,
  "location": string or null,
  "description": string or null,
  "recurrence": object or null
}}

Rules:
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
pattern allows.
- If the message is not a request to create an event, set "error" to a \
one-sentence explanation and set every other field to null or false."""


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


def parse_event(text: str, cfg: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Ask the LLM for a strict JSON event. Raises RuntimeError on failure."""
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    default_minutes=cfg["default_minutes"]
                ),
            },
            {"role": "user", "content": build_user_prompt(text, now, cfg["timezone"])},
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


def format_confirmation(event: dict[str, Any], tz_name: str) -> str:
    """Human-readable receipt for the created event."""
    tz = ZoneInfo(tz_name)
    start, end = event["start"], event["end"]

    if "date" in start:
        start_date = dt.date.fromisoformat(start["date"])
        # End date came back exclusive; show the inclusive last day.
        end_date = dt.date.fromisoformat(end["date"]) - dt.timedelta(days=1)
        if end_date <= start_date:
            when = start_date.strftime("%a, %b %-d, %Y") + " · all day"
        else:
            when = (
                f"{start_date.strftime('%a, %b %-d')} – "
                f"{end_date.strftime('%a, %b %-d, %Y')} · all day"
            )
    else:
        start_dt = dt.datetime.fromisoformat(start["dateTime"]).astimezone(tz)
        end_dt = dt.datetime.fromisoformat(end["dateTime"]).astimezone(tz)
        when = (
            f"{start_dt.strftime('%a, %b %-d, %Y')} · "
            f"{start_dt.strftime('%-I:%M %p')} – {end_dt.strftime('%-I:%M %p')}"
        )

    lines = [
        f"✅ <b>{html.escape(event.get('summary', 'Event'))}</b>",
        f"🗓 {html.escape(when)}",
    ]
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
        send_message(cfg["bot_token"], chat_id, f"⚠️ {html.escape(str(exc))}")
        return

    if parsed.get("error"):
        send_message(
            cfg["bot_token"], chat_id, f"🤔 {html.escape(str(parsed['error']))}"
        )
        return

    try:
        body = build_event_body(parsed, cfg)
    except RuntimeError as exc:
        log.error("Bad event from LLM: %s (%s)", exc, parsed)
        send_message(cfg["bot_token"], chat_id, f"⚠️ {html.escape(str(exc))}")
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
        send_message(
            cfg["bot_token"],
            chat_id,
            "⚠️ Couldn't add the event to your calendar:\n"
            f"<code>{html.escape(str(exc)[:400])}</code>",
        )
        return

    send_message(cfg["bot_token"], chat_id, format_confirmation(event, cfg["timezone"]))


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
