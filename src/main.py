"""Calendar Connect: plain English in, a Google Calendar event out.

Entry point for a 2nd-gen Google Cloud Function (HTTP trigger). It does one
thing: work out which channel a request belongs to and hand it over.

* ``/event``  channels/api.py       -- the macOS hotkey and the iOS Shortcut
* everything else
              channels/telegram.py  -- Telegram's webhook, which posts to the
                                       bare function URL

Both channels run the same handlers (handlers.py). All that differs is how a
caller proves who it is, and how the answer gets back to them.

Configuration is all environment variables; see config.py and README.md.
"""

from __future__ import annotations

import logging

import functions_framework

from channels import api, telegram

logging.basicConfig(level=logging.INFO)


@functions_framework.http
def handle_request(request):
    if (request.path or "/").rstrip("/") == "/event":
        return api.handle(request)
    return telegram.handle(request)
