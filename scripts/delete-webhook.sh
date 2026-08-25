#!/usr/bin/env bash
# Unregister the webhook. The bot stops responding until you set it again.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require curl
require gcloud
require terraform

TOKEN="$(secret telegram-bot-token)"
curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/deleteWebhook"
echo
