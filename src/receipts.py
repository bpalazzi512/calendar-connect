"""Turning an event, or a bit of help, into something to say.

Everything here builds a Message. No channel-specific markup: whether the
title comes out bold, monospaced or plain is the renderer's business.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from events import event_bounds
from messages import Message, Span, bold, code

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

_HELP_CREATE = (
    "dentist next Tuesday 3pm",
    "lunch with Sam Thursday 12:30 at Zuni",
    "flight to Denver Oct 4, all day",
    "standup tomorrow 9:15am for 15 minutes",
    "book club every Saturday 7pm",
    "gym Mon Wed Fri 6am for 8 weeks",
)
_HELP_UPDATE = (
    "move the dentist to 4pm",
    "push standup back 15 minutes",
    "lunch with Sam is at Zuni now",
    "make book club an hour and a half",
    "rename book club to reading group",
)


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
    all_day, start, end = event_bounds(event, ZoneInfo(tz_name))
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
    extra: Sequence[Sequence[str | Span]] = (),
) -> Message:
    """Receipt for an event we just created or changed."""
    message = Message(link=event.get("htmlLink") or None)
    message.line(icon, " ", bold(event.get("summary") or "Event"))
    message.line("🗓 ", describe_when(event, tz_name))
    message.extend(extra)
    for rule in event.get("recurrence") or []:
        if str(rule).startswith("RRULE:"):
            message.line("🔁 ", describe_recurrence(rule))
    if event.get("location"):
        message.line("📍 ", str(event["location"]))
    return message


def help_message() -> Message:
    message = Message()
    message.line("Describe an event in plain English and I'll put it on your calendar.")
    message.blank()
    message.line("Examples:")
    for example in _HELP_CREATE:
        message.line("• ", code(example))
    message.blank()
    message.line("Already on the calendar? Say what should change and I'll find it:")
    for example in _HELP_UPDATE:
        message.line("• ", code(example))
    return message
