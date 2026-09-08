"""Talking to Google Calendar: credentials, the client, and finding events."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any
from zoneinfo import ZoneInfo

import google.auth
import requests
from google.auth import impersonated_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build as build_google_client

log = logging.getLogger("calendar-connect")

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
METADATA_EMAIL_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/email"
)

# How far to look for an event the user wants to change, when the message
# doesn't say. Wide by default because the search is keyword-driven; narrow
# when there's no keyword to search on and we have to list everything.
SEARCH_PAST_DAYS = 30
SEARCH_FUTURE_DAYS = 365
BROWSE_PAST_DAYS = 7
BROWSE_FUTURE_DAYS = 60
MAX_CANDIDATES = 25


_service = None  # cached across warm invocations


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


def service():
    global _service
    if _service is None:
        _service = build_google_client(
            "calendar",
            "v3",
            credentials=_calendar_credentials(),
            cache_discovery=False,
        )
    return _service


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
        items = service().events().list(**params).execute().get("items", [])
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
