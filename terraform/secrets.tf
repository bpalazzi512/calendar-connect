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
    ["${var.function_name}-api-token"],
    local.use_sa_key ? ["${var.function_name}-calendar-sa-key"] : [],
  )
}

# Token for the /event API the Mac hotkey and the iOS Shortcut call.
# Generated rather than asked for: there's nothing to choose here, and it
# saves a variable you'd otherwise have to invent and keep out of git.
# Read it back with ./scripts/api-token.sh.
#
# Kept out of local.secrets, and so out of that for_each, so the map stays
# built from plain variables.
resource "random_password" "api_token" {
  length = 48
  # Alphanumeric only: this gets pasted into a curl header, a shell script
  # and a Shortcuts text field, none of which need the quoting practice.
  special = false
}

resource "google_secret_manager_secret" "api_token" {
  project   = var.project_id
  secret_id = "${var.function_name}-api-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "api_token" {
  secret      = google_secret_manager_secret.api_token.id
  secret_data = random_password.api_token.result
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
    google_secret_manager_secret.api_token,
    google_secret_manager_secret.calendar_sa_key,
  ]
}
