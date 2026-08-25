#!/usr/bin/env bash
# Ask Telegram what webhook it has registered, and whether delivery is failing.
# 'last_error_message' is the first place to look when the bot goes quiet.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require curl
require gcloud
require terraform

TOKEN="$(secret telegram-bot-token)"
curl -sS "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" |
  { python3 -m json.tool 2>/dev/null || cat; }
