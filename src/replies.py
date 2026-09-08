"""The seam between the handlers and whoever is listening.

A handler says things and marks the ones that didn't work out. It never learns
which channel it's serving, which is what lets one set of handlers back both
the Telegram bot and the JSON API.
"""

from __future__ import annotations

from messages import Message, as_message


class Reply:
    def say(self, message: Message | str) -> None:
        self._emit(as_message(message))

    def fail(self, message: Message | str) -> None:
        """Say something, and record that the request didn't work out."""
        self._emit(as_message(message))

    def _emit(self, message: Message) -> None:
        raise NotImplementedError

    def typing(self) -> None:
        """Best-effort 'working on it', where the channel has such a thing."""
