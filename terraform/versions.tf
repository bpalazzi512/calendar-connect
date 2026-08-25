terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Budgets hang off the billing account, not a project, so the Billing Budgets
# API has no project to bill quota against and refuses user (ADC) credentials
# unless you name one explicitly. These two settings make the provider send an
# X-Goog-User-Project header on those calls.
#
# Aliased rather than set on the default provider so it only affects
# google_billing_budget -- every other resource keeps the behaviour it was
# created with. Using it needs serviceusage.services.use on the project, which
# Owner grants.
provider "google" {
  alias   = "billing"
  project = var.project_id
  region  = var.region

  user_project_override = true
  billing_project       = var.project_id
}
