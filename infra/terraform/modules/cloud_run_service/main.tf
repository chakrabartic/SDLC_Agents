# One Cloud Run v2 service (docs/12 §2): per-service SA, internal ingress by default, Cloud Run IAP, invoker granted
# ONLY to the explicit members passed in (IAP service agent for IAP-protected services) — never allUsers.
# Provider support CONFIRMED: iap_enabled (GA, hashicorp/google >= 7.21), functional_type (google-beta, Preview).

resource "google_cloud_run_v2_service" "this" {
  provider = google-beta

  project             = var.project_id
  location            = var.region
  name                = var.name
  ingress             = var.ingress
  iap_enabled         = var.iap_enabled
  functional_type     = var.functional_type
  launch_stage        = var.functional_type == null ? null : "BETA"
  deletion_protection = var.deletion_protection
  labels              = merge(var.labels, { service = var.name })

  template {
    service_account                  = var.service_account_email
    timeout                          = var.request_timeout
    max_instance_request_concurrency = var.concurrency
    session_affinity                 = var.session_affinity
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    labels                           = merge(var.labels, { service = var.name })

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    dynamic "vpc_access" {
      for_each = var.vpc_subnetwork == null ? [] : [var.vpc_subnetwork]
      content {
        egress = var.vpc_egress
        network_interfaces {
          subnetwork = vpc_access.value
        }
      }
    }

    containers {
      image = var.image

      ports {
        container_port = var.container_port
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = var.cpu_idle
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = var.env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env_vars
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }
    }
  }

  lifecycle {
    # CI owns revisions (image rollout, traffic splitting 10% -> 50% -> 100%, docs/12 §4 stage 11).
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      traffic,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_binding" "invoker" {
  count    = length(var.invoker_members) > 0 ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  members  = var.invoker_members
}

resource "google_iap_web_cloud_run_service_iam_binding" "accessor" {
  count                  = var.iap_enabled && length(var.iap_accessors) > 0 ? 1 : 0
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.this.name
  role                   = "roles/iap.httpsResourceAccessor"
  members                = var.iap_accessors
}
