resource "google_cloudfunctions2_function" "bot" {
  project     = var.project_id
  name        = var.function_name
  location    = var.region
  description = "Turns Telegram messages into Google Calendar events."

  build_config {
    runtime     = var.runtime
    entry_point = "telegram_webhook"

    # Our repo, with the cleanup policy, instead of the auto-created one.
    docker_repository = google_artifact_registry_repository.images.id

    source {
      storage_source {
        bucket = google_storage_bucket.source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    available_memory      = "256Mi"
    timeout_seconds       = 60
    max_instance_count    = var.max_instance_count
    min_instance_count    = var.min_instance_count
    ingress_settings      = "ALLOW_ALL"
    service_account_email = google_service_account.bot.email

    environment_variables = {
      ALLOWED_TELEGRAM_USER_ID = var.allowed_telegram_user_id
      LLM_BASE_URL             = var.llm_base_url
      LLM_MODEL                = var.llm_model
      CALENDAR_ID              = var.calendar_id
      TIMEZONE                 = var.timezone
      DEFAULT_EVENT_MINUTES    = tostring(var.default_event_minutes)
      SERVICE_ACCOUNT_EMAIL    = google_service_account.bot.email
    }

    secret_environment_variables {
      key        = "TELEGRAM_BOT_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.this["telegram-bot-token"].secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "TELEGRAM_WEBHOOK_SECRET"
      project_id = var.project_id
      secret     = google_secret_manager_secret.this["telegram-webhook-secret"].secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "LLM_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.this["llm-api-key"].secret_id
      version    = "latest"
    }

    # Only mounted when you opted into JSON-key auth.
    dynamic "secret_environment_variables" {
      for_each = local.use_sa_key ? [1] : []
      content {
        key        = "GOOGLE_SA_KEY_JSON"
        project_id = var.project_id
        secret     = google_secret_manager_secret.calendar_sa_key[0].secret_id
        version    = "latest"
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_storage_bucket_iam_member.build_reads_source,
    google_artifact_registry_repository_iam_member.build_writes,
    google_artifact_registry_repository_iam_member.run_reads,
    google_secret_manager_secret_version.this,
    time_sleep.wait_for_iam,
  ]
}

# Telegram calls this URL unauthenticated, so the endpoint has to be public.
# What actually protects it is the shared secret header the function checks
# before doing anything, plus the allowed-user-id check after that.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.bot.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
