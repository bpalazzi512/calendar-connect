"""One message in, one answer out -- whichever channel asked.

Nothing here knows about Telegram, HTTP, or HTML. It takes text and a Reply
to talk into, and that is the whole of its contact with the outside world.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import calendar_api
import llm
from events import build_event_body, build_update_patch, shift_event_times
from messages import Message, Span, bold, code, strike
from receipts import describe_when, format_confirmation, help_message
from replies import Reply

log = logging.getLogger("calendar-connect")


def _complain(reply: Reply, message: str) -> None:
    reply.fail(f"⚠️ {message}")


def _calendar_failure(reply: Reply, what: str, exc: Exception) -> None:
    reply.fail(
        Message()
        .line(f"⚠️ Couldn't {what}:")
        .line(code(str(exc)[:400]))
    )


def handle_create(reply: Reply, cfg: dict[str, Any], parsed: dict[str, Any]) -> None:
    try:
        body = build_event_body(parsed, cfg)
    except RuntimeError as exc:
        log.error("Bad event from LLM: %s (%s)", exc, parsed)
        _complain(reply, str(exc))
        return

    try:
        event = (
            calendar_api.service()
            .events()
            .insert(calendarId=cfg["calendar_id"], body=body)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar insert failed")
        _calendar_failure(reply, "add the event to your calendar", exc)
        return

    reply.say(format_confirmation(event, cfg["timezone"]))


def handle_update(
    text: str,
    reply: Reply,
    cfg: dict[str, Any],
    parsed: dict[str, Any],
    now: dt.datetime,
) -> None:
    """Find the event the message is about, then patch it."""
    search = parsed.get("search")
    if not isinstance(search, dict):
        search = {}

    try:
        candidates = calendar_api.find_candidates(search, cfg, now)
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar search failed")
        _calendar_failure(reply, "search your calendar", exc)
        return

    if not candidates:
        reply.fail("🤔 I couldn't find an event like that on your calendar.")
        return

    try:
        decision = llm.choose_update(text, candidates, cfg, now)
    except RuntimeError as exc:
        log.exception("LLM match failed")
        _complain(reply, str(exc))
        return

    if decision.get("error"):
        reply.fail(f"🤔 {decision['error']}")
        return

    try:
        index = int(decision.get("match"))
        event = candidates[index]
    except (TypeError, ValueError, IndexError):
        log.error("LLM picked no usable candidate: %s", decision)
        reply.fail(
            "🤔 I couldn't tell which event you meant. Try naming it "
            "the way it appears on your calendar."
        )
        return

    changes = decision.get("changes")
    if not isinstance(changes, dict):
        changes = {}

    try:
        patch, shift = build_update_patch(event, changes, cfg)
    except RuntimeError as exc:
        log.error("Bad change set from LLM: %s (%s)", exc, decision)
        _complain(reply, str(exc))
        return

    if not patch:
        reply.fail(
            Message().line(
                "🤔 I found ",
                bold(event.get("summary") or "that event"),
                " but couldn't tell what to change about it.",
            )
        )
        return

    series = str(decision.get("scope") or "this").lower() == "all"
    series = series and bool(event.get("recurringEventId"))
    target = event["id"]

    try:
        if series:
            target = event["recurringEventId"]
            master = (
                calendar_api.service()
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
            calendar_api.service()
            .events()
            .patch(calendarId=cfg["calendar_id"], eventId=target, body=patch)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - always report back to the user
        log.exception("Calendar update failed")
        _calendar_failure(reply, "update that event", exc)
        return

    extra: list[Sequence[str | Span]] = []
    after = describe_when(updated, cfg["timezone"])
    if after != before:
        extra.append(("↩️ ", strike(before)))
    if event.get("recurringEventId"):
        extra.append(
            ("🔁 every occurrence",)
            if series
            else ("🔂 this occurrence only",)
        )

    reply.say(
        format_confirmation(updated, cfg["timezone"], icon="✏️", extra=extra)
    )


def handle_text(text: str, reply: Reply, cfg: dict[str, Any]) -> None:
    """Parse one message and act on it. Never raises."""
    command = text.strip().split()[0].lower().split("@")[0] if text.strip() else ""
    if command in {"/start", "/help"}:
        reply.say(help_message())
        return

    reply.typing()

    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))
    try:
        parsed = llm.parse_event(text, cfg, now)
    except RuntimeError as exc:
        log.exception("LLM parse failed")
        _complain(reply, str(exc))
        return

    if parsed.get("error"):
        reply.fail(f"🤔 {parsed['error']}")
        return

    if str(parsed.get("intent") or "create").lower() == "update":
        handle_update(text, reply, cfg, parsed, now)
        return
    handle_create(reply, cfg, parsed)
