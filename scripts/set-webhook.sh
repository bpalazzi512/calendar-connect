#!/usr/bin/env bash
# Point the Telegram bot at the deployed function.
# Only needed if you set register_webhook = false, or if you want to re-register.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require curl
require gcloud
require terraform

URL="$(tf_output function_url)"
TOKEN="$(secret telegram-bot-token)"
WEBHOOK_SECRET="$(secret telegram-webhook-secret)"

echo "Registering webhook -> $URL"
curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  --data-urlencode "url=${URL}" \
  --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message"]' \
  --data-urlencode "drop_pending_updates=true"
echo
