data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Cloud Build runs as the default compute service account for 2nd-gen functions.
  build_service_account = "${data.google_project.this.number}-compute@developer.gserviceaccount.com"

  # The agent that pulls the container image when an instance starts.
  run_service_agent = "service-${data.google_project.this.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "iamcredentials.googleapis.com",
    "calendar-json.googleapis.com",
    "logging.googleapis.com",
    "eventarc.googleapis.com",
    "pubsub.googleapis.com",
    "billingbudgets.googleapis.com",
  ])

  project = var.project_id
  service = each.value

  # Leave the APIs on if this stack is torn down; other things may use them.
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Runtime service account
# ---------------------------------------------------------------------------

resource "google_service_account" "bot" {
  project      = var.project_id
  account_id   = var.function_name
  display_name = "Calendar bot function runtime"
  description  = "Runs the Telegram->Calendar function. Share your calendar with this address."

  depends_on = [google_project_service.apis]
}

# The metadata-server token this function gets only carries the cloud-platform
# scope, which does not cover Calendar (a Workspace API). Letting the account
# mint tokens for itself lets the code request the Calendar scope explicitly,
# so we never have to create or store a JSON key.
resource "google_service_account_iam_member" "self_token_creator" {
  service_account_id = google_service_account.bot.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.bot.email}"
}

resource "google_project_iam_member" "bot_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.bot.email}"
}

# ---------------------------------------------------------------------------
# Cloud Build permissions (used to build the function image)
# ---------------------------------------------------------------------------

resource "google_project_iam_member" "build_roles" {
  for_each = toset([
    "roles/cloudbuild.builds.builder",
    "roles/logging.logWriter",
    "roles/artifactregistry.writer",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${local.build_service_account}"

  depends_on = [google_project_service.apis]
}

# IAM changes take a little while to propagate; a first apply that races them
# fails with a confusing build error.
resource "time_sleep" "wait_for_iam" {
  create_duration = "60s"

  depends_on = [
    google_project_iam_member.build_roles,
    google_project_iam_member.bot_log_writer,
    google_service_account_iam_member.self_token_creator,
    google_secret_manager_secret_iam_member.bot_access,
  ]
}

# ---------------------------------------------------------------------------
# Function source
# ---------------------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "google_storage_bucket" "source" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.function_name}-src-${random_id.bucket_suffix.hex}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "build_reads_source" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.build_service_account}"
}

data "archive_file" "source" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/function-source.zip"
  excludes    = ["__pycache__", ".pytest_cache", ".DS_Store"]
}

resource "google_storage_bucket_object" "source" {
  # The hash in the name makes a code change produce a new object, which is
  # what tells Cloud Functions to rebuild.
  name   = "source/${data.archive_file.source.output_md5}.zip"
  bucket = google_storage_bucket.source.name
  source = data.archive_file.source.output_path
}
