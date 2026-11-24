# CI/CD support (docs/12 §4, referenced by cloudbuild.yaml): Artifact Registry repo (immutable tags, CMEK,
# cleanup), the CI service account (no keys; Cloud Build / WIF), and an optional Cloud Build trigger.

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  description   = "PM Agent images (deploy/Dockerfile, one per component)"
  format        = "DOCKER"
  kms_key_name  = var.kms_key_name
  labels        = var.labels

  docker_config {
    immutable_tags = true # images are referenced by $SHORT_SHA; never retagged (supply chain, T10)
  }

  cleanup_policies {
    id     = "delete-untagged-30d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "2592000s"
    }
  }

  cleanup_policies {
    id     = "keep-recent-50"
    action = "KEEP"
    most_recent_versions {
      keep_count = 50
    }
  }
}

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "sa-ci-build"
  display_name = "PM Agent ${var.env} — CI/CD (Cloud Build)"
  description  = "Builds images, deploys Cloud Run revisions and Agent Runtime engines; never holds data-plane roles."
}

resource "google_artifact_registry_repository_iam_member" "ci_writer" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/run.developer",
    "roles/aiplatform.user",
    "roles/cloudbuild.builds.builder",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_service_account_iam_member" "ci_act_as" {
  for_each           = toset(var.runtime_service_account_emails)
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_cloudbuild_trigger" "main" {
  count           = var.cloudbuild_repository == null ? 0 : 1
  project         = var.project_id
  location        = var.region
  name            = "pm-agent-${var.env}-main"
  description     = "Gates 1-8 (docs/12 §4) on pushes to main"
  filename        = "cloudbuild.yaml"
  service_account = google_service_account.ci.id

  repository_event_config {
    repository = var.cloudbuild_repository
    push {
      branch = var.trigger_branch_regex
    }
  }

  substitutions = {
    _ENV              = var.env
    _REGION           = var.region
    _AR_REPO          = google_artifact_registry_repository.images.repository_id
    _AGENT_RUNTIME_SA = google_service_account.ci.email
  }
}
