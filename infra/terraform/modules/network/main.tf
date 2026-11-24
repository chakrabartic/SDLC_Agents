# VPC, subnets, PSC network attachments, Cloud NAT with static egress IPs, firewall, private DNS (docs/12 §3).
# Only public ingress points are pm-workbench (IAP) and signal-event-gateway (HTTPS LB + Cloud Armor); everything
# else is private (docs/03 §5).

locals {
  prefix = "pm-agent-${var.env}"
}

resource "google_compute_network" "vpc" {
  project                         = var.project_id
  name                            = var.network_name
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "workloads" {
  project                  = var.project_id
  name                     = "${local.prefix}-workloads"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.workloads_cidr
  private_ip_google_access = true # Cloud Run internal ingress + Google APIs without public egress

  secondary_ip_range {
    range_name    = "gke-pods"
    ip_cidr_range = var.gke_pods_cidr
  }

  secondary_ip_range {
    range_name    = "gke-services"
    ip_cidr_range = var.gke_services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Agent Runtime -> VPC (psc_interface_config on each reasoning engine, docs/03 §5).
resource "google_compute_subnetwork" "psc_runtime" {
  project                  = var.project_id
  name                     = "${local.prefix}-psc-runtime"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.psc_runtime_cidr
  private_ip_google_access = true
}

# Agent Gateway (Agent-to-Anywhere) -> VPC; required for VPC-SC enforcement on the gateway (connectivity template).
resource "google_compute_subnetwork" "psc_gateway" {
  project                  = var.project_id
  name                     = "${local.prefix}-psc-gateway"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.psc_gateway_cidr
  private_ip_google_access = true
}

# Regional internal Application LB (GKE Gateway gke-l7-rilb) for mcp-correlation needs a proxy-only subnet.
resource "google_compute_subnetwork" "proxy_only" {
  project       = var.project_id
  name          = "${local.prefix}-proxy-only"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.proxy_only_cidr
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

resource "google_compute_network_attachment" "agent_runtime" {
  project               = var.project_id
  name                  = "${local.prefix}-na-agent-runtime"
  region                = var.region
  description           = "PSC interface for Agent Runtime deployments (pm-agent-core, signal-ingestion, commit-executor)"
  connection_preference = "ACCEPT_AUTOMATIC"
  subnetworks           = [google_compute_subnetwork.psc_runtime.id]
}

resource "google_compute_network_attachment" "agent_gateway" {
  project               = var.project_id
  name                  = "${local.prefix}-na-agent-gateway"
  region                = var.region
  description           = "PSC interface for the Agent Gateway egress connectivity template (vpc_egress = ALL_TRAFFIC)"
  connection_preference = "ACCEPT_AUTOMATIC"
  subnetworks           = [google_compute_subnetwork.psc_gateway.id]
}

# ---------------------------------------------------------------- Cloud NAT (static egress IPs)
resource "google_compute_address" "nat" {
  count        = var.nat_static_ip_count
  project      = var.project_id
  name         = "${local.prefix}-nat-${count.index}"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
  labels       = var.labels
}

resource "google_compute_router" "router" {
  project = var.project_id
  name    = "${local.prefix}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  project                            = var.project_id
  name                               = "${local.prefix}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = google_compute_address.nat[*].self_link
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.workloads.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# ---------------------------------------------------------------- Firewall (implicit deny-ingress stays in place)
resource "google_compute_firewall" "allow_internal" {
  project   = var.project_id
  name      = "${local.prefix}-allow-internal"
  network   = google_compute_network.vpc.id
  direction = "INGRESS"
  priority  = 1000
  source_ranges = [
    var.workloads_cidr,
    var.gke_pods_cidr,
    var.psc_runtime_cidr,
    var.psc_gateway_cidr,
    var.proxy_only_cidr,
  ]

  allow {
    protocol = "tcp"
    ports    = ["443", "5432", "8080", "8443"]
  }
}

# Google front-end health checks for the event-gateway HTTPS LB and the correlation-engine internal LB.
resource "google_compute_firewall" "allow_health_checks" {
  project       = var.project_id
  name          = "${local.prefix}-allow-health-checks"
  network       = google_compute_network.vpc.id
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]

  allow {
    protocol = "tcp"
    ports    = ["8080", "8443"]
  }
}

# ---------------------------------------------------------------- Internal addresses + private DNS
# Static internal VIP for the correlation-engine internal LB (mcp-correlation), referenced by the k8s Service
# annotation networking.gke.io/load-balancer-ip-addresses and registered in Agent Registry by DNS name.
resource "google_compute_address" "correlation_ilb" {
  project      = var.project_id
  name         = "${local.prefix}-correlation-ilb"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.workloads.id
  purpose      = "SHARED_LOADBALANCER_VIP"
  labels       = var.labels
}

resource "google_dns_managed_zone" "private" {
  project     = var.project_id
  name        = "${local.prefix}-internal"
  dns_name    = var.private_dns_domain
  description = "Private names for PM Agent internal endpoints (AlloyDB PSC endpoint, mcp-correlation)"
  visibility  = "private"
  labels      = var.labels

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.id
    }
  }
}

resource "google_dns_record_set" "correlation" {
  project      = var.project_id
  managed_zone = google_dns_managed_zone.private.name
  name         = "mcp-correlation.${var.private_dns_domain}"
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.correlation_ilb.address]
}
