"""The Telegram channel: the Bot API, the HTML renderer, and the webhook.

This is the only module in the project that knows HTML exists. Handlers say
what they mean in plain spans; the markup is added here, on the way out, for
the one audience that wants it.
"""

from __future__ import annotations

import hmac
import html
import logging

import requests

import config
import handlers
from messages import BOLD, CODE, STRIKE, Message
from replies import Reply

log = logging.getLogger("calendar-connect")

TELEGRAM_API = "https://api.telegram.org"

_TAGS = {BOLD: "b", CODE: "code", STRIKE: "s"}


def render_html(message: Message) -> str:
    """A Message as Telegram's flavour of HTML.

    Escaping happens here and only here, on text that has never been through
    another renderer, so it can be neither forgotten nor doubled up.
    """
    lines = []
    for line in message.lines:
        rendered = []
        for span in line:
            escaped = html.escape(span.text)
            tag = _TAGS.get(span.style)
            rendered.append(f"<{tag}>{escaped}</{tag}>" if tag else escaped)
        lines.append("".join(rendered))
    if message.link:
        lines.append(f'<a href="{html.escape(message.link)}">Open in Calendar</a>')
    return "\n".join(lines)


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


class TelegramReply(Reply):
    def __init__(self, bot_token: str, chat_id: int | str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _emit(self, message: Message) -> None:
        send_message(self.bot_token, self.chat_id, render_html(message))

    def typing(self) -> None:
        send_typing(self.bot_token, self.chat_id)


def handle(request):
    """Telegram's webhook. Always answers 200 so Telegram doesn't retry."""
    if request.method == "GET":
        return ("calendar-connect is up", 200)
    if request.method != "POST":
        return ("", 405)

    try:
        cfg = config.load()
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

    reply = TelegramReply(cfg["bot_token"], chat_id)

    text = message.get("text")
    if not text:
        reply.say("Send me a text message describing the event.")
        return ("ignored", 200)

    try:
        handlers.handle_text(text, reply, cfg)
    except Exception:  # noqa: BLE001 - a 500 makes Telegram retry; don't
        log.exception("Unhandled error")
        reply.fail("⚠️ Something went wrong. Check the logs.")

    return ("ok", 200)
