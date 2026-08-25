# Cloud Functions would otherwise push builds into a repo it creates itself
# ("gcf-artifacts"), which we can't attach a retention policy to from here.
# Owning the repo lets us expire old images instead of paying to keep every
# build forever -- each one is a few hundred MB against a 0.5 GB free tier.
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.function_name}-images"
  format        = "DOCKER"
  description   = "Container images built from src/ for the calendar bot."

  # false = the policies actually delete. Flip to true to see what they'd
  # remove (logged, nothing deleted) before trusting them.
  cleanup_policy_dry_run = false

  # KEEP rules win over DELETE rules, so this guarantees the image the service
  # is currently running never expires, however long you go between deploys.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.image_keep_count
    }
  }

  cleanup_policies {
    id     = "expire-old"
    action = "DELETE"
    condition {
      # A protobuf Duration: seconds only, no "7d" or "168h".
      older_than = "${var.image_retention_days * 86400}s"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "build_writes" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${local.build_service_account}"
}

# Without this, instances fail to start with an image-pull error.
resource "google_artifact_registry_repository_iam_member" "run_reads" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${local.run_service_agent}"
}
