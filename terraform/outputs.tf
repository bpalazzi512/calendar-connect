output "function_url" {
  description = "HTTPS endpoint Telegram POSTs to."
  value       = google_cloudfunctions2_function.bot.service_config[0].uri
}

output "project_id" {
  description = "Project the bot is deployed in."
  value       = var.project_id
}

output "region" {
  description = "Region the function runs in."
  value       = var.region
}

output "function_name" {
  description = "Name of the function / Cloud Run service."
  value       = var.function_name
}

output "service_account_email" {
  description = "Share your calendar with this address ('Make changes to events')."
  value       = google_service_account.bot.email
}

output "calendar_id" {
  description = "Calendar the bot writes to."
  value       = var.calendar_id
}

output "budget_alert" {
  description = "Whether the monthly budget alert was created."
  value = length(google_billing_budget.guardrail) > 0 ? (
    "${var.budget_currency} ${var.budget_amount}/month, alerting at 50/90/100%"
  ) : "not created (billing_account_id is empty)"
}

output "image_repository" {
  description = "Artifact Registry repo holding the function's images, with the cleanup policy applied."
  value       = google_artifact_registry_repository.images.id
}

output "logs_command" {
  description = "Tail the function's logs."
  value       = "gcloud beta run services logs tail ${var.function_name} --project ${var.project_id} --region ${var.region}"
}

output "webhook_info_command" {
  description = "Ask Telegram what webhook it currently has registered."
  value       = "./scripts/webhook-info.sh"
}
