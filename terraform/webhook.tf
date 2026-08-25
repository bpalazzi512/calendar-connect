# Point the Telegram bot at the deployed function. This is the one resource
# that reaches outside Google Cloud; set register_webhook = false to do it by
# hand with scripts/set-webhook.sh instead.
resource "null_resource" "telegram_webhook" {
  count = var.register_webhook ? 1 : 0

  triggers = {
    url         = google_cloudfunctions2_function.bot.service_config[0].uri
    secret_hash = sha256(var.telegram_webhook_secret)
    token_hash  = sha256(var.telegram_bot_token)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]

    # Passed as environment so the token never lands in a process listing.
    environment = {
      BOT_TOKEN      = var.telegram_bot_token
      WEBHOOK_URL    = google_cloudfunctions2_function.bot.service_config[0].uri
      WEBHOOK_SECRET = var.telegram_webhook_secret
    }

    command = <<-EOT
      set -euo pipefail
      response=$(curl -sS -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
        --data-urlencode "url=$WEBHOOK_URL" \
        --data-urlencode "secret_token=$WEBHOOK_SECRET" \
        --data-urlencode 'allowed_updates=["message"]' \
        --data-urlencode "drop_pending_updates=true")
      echo "setWebhook: $response"
      case "$response" in
        *'"ok":true'*) ;;
        *) echo "Telegram rejected setWebhook" >&2; exit 1 ;;
      esac
    EOT
  }

  depends_on = [google_cloud_run_v2_service_iam_member.public_invoker]
}
