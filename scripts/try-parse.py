#!/usr/bin/env python3
"""Run one message through the LLM locally and print the event that would be
created. Touches the LLM only -- never Telegram, never your calendar.

    pip install -r src/requirements.txt
    export LLM_API_KEY=sk-...
    ./scripts/try-parse.py "dentist next Tuesday 3pm"

Useful for checking your model name and API key before deploying, and for
seeing how the prompt handles a phrasing that surprised you.
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Values the real function gets from Terraform; only the LLM ones matter here.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unused")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "unused")
os.environ.setdefault("ALLOWED_TELEGRAM_USER_ID", "0")
os.environ.setdefault("CALENDAR_ID", "unused")

import main  # noqa: E402


def run(text: str) -> int:
    cfg = main._config()
    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))

    print(f"model:    {cfg['llm_model']}  @  {cfg['llm_base_url']}")
    print(f"timezone: {cfg['timezone']}  (now {now:%Y-%m-%d %H:%M %A})")
    print(f"message:  {text}\n")

    try:
        parsed = main.parse_event(text, cfg, now)
    except RuntimeError as exc:
        print(f"LLM error: {exc}")
        return 1

    print("LLM JSON:")
    print(json.dumps(parsed, indent=2))

    if parsed.get("error"):
        print(f"\nNot an event: {parsed['error']}")
        return 0

    try:
        body = main.build_event_body(parsed, cfg)
    except RuntimeError as exc:
        print(f"\nUnusable event: {exc}")
        return 1

    print("\nCalendar event that would be inserted:")
    print(json.dumps(body, indent=2))
    print("\nConfirmation you'd get back:")
    print(main.format_confirmation(body, cfg["timezone"]))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(run(" ".join(sys.argv[1:])))
