# GKE Autopilot private cluster for the data plane (docs/03 §2): signal-event-gateway (public HTTPS LB + Cloud Armor),
# correlation-engine (internal LB, mcp-correlation) and the dls-catalog-sync / re-clustering CronJobs.
# Workload manifests live in deploy/k8s (Kustomize); KSAs are annotated for Workload Identity Federation.

resource "google_container_cluster" "autopilot" {
  provider = google

  project             = var.project_id
  name                = "pm-agent-${var.env}"
  location            = var.region
  enable_autopilot    = true
  network             = var.network_id
  subnetwork          = var.subnetwork_id
  deletion_protection = var.deletion_protection
  resource_labels     = var.labels

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true # control plane private endpoint only (docs/03 §5)
    master_ipv4_cidr_block  = var.master_ipv4_cidr_block

    master_global_access_config {
      enabled = false
    }
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.master_authorized_cidrs
      content {
        display_name = cidr_blocks.key
        cidr_block   = cidr_blocks.value
      }
    }
  }

  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE" # signed images only once the project policy requires attestations (T10)
  }

  dynamic "database_encryption" {
    for_each = var.database_encryption_key == null ? [] : [var.database_encryption_key]
    content {
      state    = "ENCRYPTED"
      key_name = database_encryption.value
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Gateway API: gke-l7-rilb (internal, TLS) in front of mcp-correlation; see deploy/k8s/base/correlation-engine.yaml.
  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }
}

# KSA -> GSA (Workload Identity Federation for GKE). The KSA side is the annotation in deploy/k8s.
resource "google_service_account_iam_member" "workload_identity" {
  for_each           = var.workload_identity_bindings
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.key}"
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${each.value.namespace}/${each.value.ksa}]"

  depends_on = [google_container_cluster.autopilot]
}

# ---------------------------------------------------------------- public webhook ingress (signal-event-gateway)
resource "google_compute_global_address" "event_gateway" {
  project = var.project_id
  name    = "pm-agent-${var.env}-event-gateway"
}

resource "google_compute_security_policy" "event_gateway" {
  project     = var.project_id
  name        = "pm-agent-${var.env}-event-gateway"
  description = "Webhook fan-in: per-IP rate limit, optional vendor allow-list; signatures verified in-app (T8)."

  rule {
    action   = "throttle"
    priority = 1000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.event_gateway_rate_limit_per_minute
        interval_sec = 60
      }
    }
    description = "Per-source-IP rate limit"
  }

  dynamic "rule" {
    for_each = length(var.event_gateway_allowed_cidrs) > 0 ? [1] : []
    content {
      action   = "allow"
      priority = 900
      match {
        versioned_expr = "SRC_IPS_V1"
        config {
          src_ip_ranges = var.event_gateway_allowed_cidrs
        }
      }
      description = "Vendor webhook source allow-list"
    }
  }

  rule {
    action   = length(var.event_gateway_allowed_cidrs) > 0 ? "deny(403)" : "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Default rule"
  }
}
