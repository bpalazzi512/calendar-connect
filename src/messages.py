"""What a handler wants to say, before any channel has styled it.

The plain words are the canonical form. A channel that wants markup adds it
while rendering -- channels/telegram.py is the only place HTML exists, and it
is the only module that needs to think about escaping. Handlers just say what
they mean, and never hold a string with a tag in it.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

PLAIN = ""
BOLD = "bold"
CODE = "code"
STRIKE = "strike"


class Span(NamedTuple):
    """A run of text, and how it would like to be emphasised."""

    text: str
    style: str = PLAIN


def bold(text: str) -> Span:
    return Span(text, BOLD)


def code(text: str) -> Span:
    return Span(text, CODE)


def strike(text: str) -> Span:
    return Span(text, STRIKE)


class Message:
    """One reply: some lines of spans, plus an optional link to the event."""

    def __init__(self, *, link: str | None = None) -> None:
        self.lines: list[list[Span]] = []
        self.link = link

    def line(self, *parts: str | Span) -> "Message":
        self.lines.append([p if isinstance(p, Span) else Span(p) for p in parts])
        return self

    def blank(self) -> "Message":
        self.lines.append([])
        return self

    def extend(self, rows: Sequence[Sequence[str | Span]]) -> "Message":
        for row in rows:
            self.line(*row)
        return self

    def __str__(self) -> str:
        return render_text(self)


def render_text(message: Message) -> str:
    """The canonical rendering: the words, no markup.

    The link is deliberately left out. A channel that can show one puts it
    where it belongs -- inline for Telegram, its own JSON field for the API.
    """
    return "\n".join("".join(span.text for span in line) for line in message.lines)


def as_message(message: Message | str) -> Message:
    """Coerce at the boundary, so a handler can say a one-liner as a string.

    Unambiguous in a way the old HTML strings weren't: plain text has no
    markup to lose.
    """
    return message if isinstance(message, Message) else Message().line(message)
