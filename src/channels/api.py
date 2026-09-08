"""The JSON channel: a bearer token in, plain text out.

Behind this sit the macOS hotkey and the iOS Shortcut. Unlike Telegram it
answers in the HTTP response, so a client needs nothing but the token -- no
account, no polling, no callback.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import config
import handlers
from messages import Message, render_text
from replies import Reply

log = logging.getLogger("calendar-connect")


class ApiReply(Reply):
    """Collects what the handlers say, for the response body."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.link: str | None = None
        self.ok = True

    def _emit(self, message: Message) -> None:
        text = render_text(message)
        if text:
            self.messages.append(text)
        if message.link and not self.link:
            self.link = message.link

    def fail(self, message: Message | str) -> None:
        self.ok = False
        super().fail(message)

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": "\n".join(self.messages),
            "link": self.link,
        }


def _json(payload: dict[str, Any], status: int = 200):
    return (
        json.dumps(payload, ensure_ascii=False),
        status,
        {"Content-Type": "application/json"},
    )


def _presented_token(request) -> str:
    """The token the caller presented, in whichever shape it arrived.

    Three are accepted, and they're equally strong -- a secret in a header,
    compared the same way. Which one you use is purely about what's convenient
    to type on the other end:

    * ``X-Api-Token: <token>``       no space anywhere, which is the one to
                                    reach for in a Shortcuts header field
    * ``Authorization: Bearer <t>``  the standard form, what curl examples use
    * ``Authorization: <token>``     the same thing without the ceremony
    """
    token = request.headers.get("X-Api-Token", "").strip()
    if token:
        return token

    header = request.headers.get("Authorization", "").strip()
    scheme, _, rest = header.partition(" ")
    if rest and scheme.lower() == "bearer":
        return rest.strip()
    return header


def handle(request):
    """POST /event.

    A request the handlers actually processed comes back 200 even when it
    didn't work out -- "I couldn't find that event" is an answer, and the
    ``ok`` field carries the verdict. Shortcuts treats any non-2xx as a failed
    action and throws the body away, so those are reserved for auth and
    protocol problems the caller has to fix.
    """
    if request.method != "POST":
        return _json({"ok": False, "message": "POST a JSON body."}, 405)

    try:
        cfg = config.load()
    except RuntimeError:
        log.exception("Bad configuration")
        return _json({"ok": False, "message": "Server is misconfigured."}, 500)

    if not cfg["api_token"]:
        log.error("API_TOKEN is unset; refusing to serve /event")
        return _json({"ok": False, "message": "API is not enabled."}, 503)

    # Compared against every accepted header shape; see _presented_token.

    if not hmac.compare_digest(_presented_token(request), cfg["api_token"]):
        log.warning("Rejected /event request with a bad or missing token")
        return _json({"ok": False, "message": "Forbidden."}, 403)

    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return _json(
            {"ok": False, "message": "Send some text describing the event."}, 400
        )

    reply = ApiReply()
    try:
        handlers.handle_text(text, reply, cfg)
    except Exception:  # noqa: BLE001 - answer the caller, don't hand them a 500
        log.exception("Unhandled error")
        reply.fail("⚠️ Something went wrong. Check the logs.")

    return _json(reply.payload())
