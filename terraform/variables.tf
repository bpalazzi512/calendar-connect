variable "project_id" {
  type        = string
  description = "Google Cloud project id to deploy into."
}

variable "region" {
  type        = string
  description = "Region for the function. Use a standard US region so Cloud Functions free-tier rules apply."
  default     = "us-central1"
}

variable "function_name" {
  type        = string
  description = "Name of the Cloud Function (also the name of the underlying Cloud Run service)."
  default     = "calendar-bot"
}

variable "runtime" {
  type        = string
  description = "Python runtime for the function."
  default     = "python312"
}

# --- Telegram -------------------------------------------------------------

variable "telegram_bot_token" {
  type        = string
  description = "Bot token from @BotFather."
  sensitive   = true
}

variable "telegram_webhook_secret" {
  type        = string
  description = "Random string Telegram sends back in the X-Telegram-Bot-Api-Secret-Token header. 1-256 chars, A-Z a-z 0-9 _ -."
  sensitive   = true

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{16,256}$", var.telegram_webhook_secret))
    error_message = "Must be 16-256 characters of A-Z, a-z, 0-9, underscore or hyphen (Telegram's allowed set)."
  }
}

variable "allowed_telegram_user_id" {
  type        = string
  description = "Your numeric Telegram user id (from @userinfobot). Messages from anyone else are ignored."

  validation {
    condition     = can(regex("^[0-9]+$", var.allowed_telegram_user_id))
    error_message = "Must be a numeric Telegram user id."
  }
}

variable "register_webhook" {
  type        = bool
  description = "Whether Terraform should call Telegram's setWebhook after deploying. Requires curl on the machine running Terraform."
  default     = true
}

# --- LLM ------------------------------------------------------------------

variable "llm_api_key" {
  type        = string
  description = "API key for the OpenAI-compatible LLM provider."
  sensitive   = true
}

variable "llm_base_url" {
  type        = string
  description = "OpenAI-compatible base URL (the part before /chat/completions)."
  default     = "https://api.deepseek.com/v1"
}

variable "llm_model" {
  type        = string
  description = "Model name to send in the chat-completions request."
  default     = "deepseek-v4-flash"
}

variable "llm_timeout_seconds" {
  type        = number
  description = <<-EOT
    How long to wait for the LLM's chat-completions response. Reasoning models
    such as deepseek-v4-flash spend nearly all of their latency on hidden
    chain-of-thought, and one ambiguous message can swing a single call between
    7s and 26s. Keep this comfortably under var.function_timeout_seconds so a
    slow parse still leaves room to insert the event and reply.
  EOT
  default     = 45

  validation {
    condition     = var.llm_timeout_seconds >= 5 && var.llm_timeout_seconds <= 540
    error_message = "Must be between 5 and 540 seconds."
  }
}

# --- Calendar -------------------------------------------------------------

variable "calendar_id" {
  type        = string
  description = "Calendar id to write to. Use your real calendar id (usually your email address), NOT 'primary'."
}

variable "timezone" {
  type        = string
  description = "IANA timezone used to interpret times in your messages, e.g. America/New_York."
  default     = "America/New_York"
}

variable "default_event_minutes" {
  type        = number
  description = "Duration for timed events when the message doesn't say how long."
  default     = 60
}

# --- Optional: JSON-key auth instead of keyless self-impersonation --------

variable "calendar_sa_key_json" {
  type        = string
  description = "Optional service account JSON key for Calendar access. Leave empty to use the keyless path (recommended)."
  sensitive   = true
  default     = ""
}

# --- Cost guardrails ------------------------------------------------------

variable "billing_account_id" {
  type        = string
  description = <<-EOT
    Billing account to create the budget alert on, e.g. "01A2B3-C4D5E6-F7G8H9"
    (`gcloud billing accounts list`). Leave empty to skip the budget -- creating
    one needs roles/billing.costsManager on the billing account itself, which
    project Owner does not give you.
  EOT
  default     = ""

  validation {
    condition     = var.billing_account_id == "" || can(regex("^[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}$", var.billing_account_id))
    error_message = "Must be empty or a billing account id like 01A2B3-C4D5E6-F7G8H9 (no 'billingAccounts/' prefix)."
  }
}

variable "budget_amount" {
  type        = number
  description = "Monthly budget in whole currency units. Alerts fire at 50%, 90% and 100% of it."
  default     = 1
}

variable "budget_currency" {
  type        = string
  description = "Currency for the budget. Must match the billing account's own currency."
  default     = "USD"
}

variable "image_retention_days" {
  type        = number
  description = "Delete container images older than this. Images are a few hundred MB each against a 0.5 GB free tier."
  default     = 7
}

variable "image_keep_count" {
  type        = number
  description = "Always keep this many recent images regardless of age, so the running one never expires."
  default     = 3

  validation {
    condition     = var.image_keep_count >= 1
    error_message = "Must keep at least 1 image, or a cleanup could delete the image the service is running."
  }
}

# --- Function sizing ------------------------------------------------------

variable "function_timeout_seconds" {
  type        = number
  description = <<-EOT
    Hard ceiling on one request. Has to outlast the LLM call plus the Calendar
    insert -- if it doesn't, Cloud Functions kills the request and the user
    gets silence instead of an error.
  EOT
  default     = 120

  validation {
    condition     = var.function_timeout_seconds >= 1 && var.function_timeout_seconds <= 3600
    error_message = "Cloud Functions 2nd gen allows 1-3600 seconds."
  }
}

variable "max_instance_count" {
  type        = number
  description = "Upper bound on concurrent instances. Low, since it's a single-user bot."
  default     = 3
}

variable "min_instance_count" {
  type        = number
  description = "Set to 1 to remove cold starts, at the cost of an always-on instance."
  default     = 0
}
