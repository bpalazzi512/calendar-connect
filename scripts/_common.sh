#!/usr/bin/env bash
# Shared helpers: pull config out of Terraform state and Secret Manager so the
# scripts don't need any secrets on the command line.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"

tf_output() {
  terraform -chdir="$TF_DIR" output -raw "$1" 2>/dev/null || {
    echo "Could not read Terraform output '$1'. Has 'terraform apply' run?" >&2
    exit 1
  }
}

secret() {
  local name="$1"
  gcloud secrets versions access latest \
    --secret="$(tf_output function_name)-$name" \
    --project="$(tf_output project_id)"
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}
