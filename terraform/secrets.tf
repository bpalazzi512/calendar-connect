locals {
  # Secrets always created. The Calendar JSON key is handled separately since
  # it's optional.
  secrets = {
    telegram-bot-token      = var.telegram_bot_token
    telegram-webhook-secret = var.telegram_webhook_secret
    llm-api-key             = var.llm_api_key
  }

  use_sa_key = trimspace(var.calendar_sa_key_json) != ""

  # Derived from variables rather than resource attributes so the for_each
  # keys below are known at plan time.
  all_secret_ids = concat(
    [for k in keys(local.secrets) : "${var.function_name}-${k}"],
    local.use_sa_key ? ["${var.function_name}-calendar-sa-key"] : [],
  )
}

resource "google_secret_manager_secret" "this" {
  for_each = local.secrets

  project   = var.project_id
  secret_id = "${var.function_name}-${each.key}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "this" {
  for_each = local.secrets

  secret      = google_secret_manager_secret.this[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret" "calendar_sa_key" {
  count = local.use_sa_key ? 1 : 0

  project   = var.project_id
  secret_id = "${var.function_name}-calendar-sa-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "calendar_sa_key" {
  count = local.use_sa_key ? 1 : 0

  secret      = google_secret_manager_secret.calendar_sa_key[0].id
  secret_data = var.calendar_sa_key_json
}

resource "google_secret_manager_secret_iam_member" "bot_access" {
  for_each = toset(local.all_secret_ids)

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bot.email}"

  depends_on = [
    google_secret_manager_secret.this,
    google_secret_manager_secret.calendar_sa_key,
  ]
}
