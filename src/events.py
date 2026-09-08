"""Reading and writing Google Calendar event resources.

Pure by design: dicts in, dicts out, no network. Most of the fiddly
correctness lives here -- inclusive versus exclusive end dates, keeping
wall-clock times across a DST boundary, and realigning a recurring series.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("calendar-connect")

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
# A weekday code, optionally prefixed with an ordinal: SA, 1MO, -1FR.
BYDAY_RE = re.compile(r"^(-?[1-5])?(MO|TU|WE|TH|FR|SA|SU)$")


def parse_local_datetime(value: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def naive(value: dt.datetime) -> dt.datetime:
    """Wall-clock, zone dropped. Arithmetic on these keeps 3pm at 3pm across a
    DST boundary, which arithmetic on the instant would not."""
    return value.replace(tzinfo=None)


def event_bounds(
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
    start_dt = parse_local_datetime(start["dateTime"], tz)
    end_dt = parse_local_datetime(end.get("dateTime") or start["dateTime"], tz)
    return False, start_dt, end_dt


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
            start_dt = parse_local_datetime(start_raw, tz)
            end_dt = parse_local_datetime(end_raw, tz) if end_raw else None
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
    was_all_day, cur_start, cur_end = event_bounds(event, tz)
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
                new_start = parse_local_datetime(start_raw, tz)
            elif was_all_day:
                raise RuntimeError("Tell me what time it should start.")
            else:
                new_start = cur_start

            if end_raw:
                new_end = parse_local_datetime(end_raw, tz)
            elif was_all_day:
                new_end = new_start + dt.timedelta(minutes=cfg["default_minutes"])
            else:
                # No new end: keep the length the event already had.
                moved = naive(new_start) - naive(cur_start)
                new_end = (naive(cur_end) + moved).replace(tzinfo=tz)
            if new_end <= new_start:
                new_end = new_start + dt.timedelta(minutes=cfg["default_minutes"])

            patch["start"] = {"dateTime": new_start.isoformat(), "timeZone": cfg["timezone"]}
            patch["end"] = {"dateTime": new_end.isoformat(), "timeZone": cfg["timezone"]}
            shift = (
                None
                if was_all_day
                else (
                    naive(new_start) - naive(cur_start),
                    naive(new_end) - naive(cur_end),
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
        naive(parse_local_datetime(start["dateTime"], tz)) + start_delta
    ).replace(tzinfo=tz)
    new_end = (naive(parse_local_datetime(end["dateTime"], tz)) + end_delta).replace(
        tzinfo=tz
    )
    return {
        "start": {"dateTime": new_start.isoformat(), "timeZone": cfg["timezone"]},
        "end": {"dateTime": new_end.isoformat(), "timeZone": cfg["timezone"]},
    }
