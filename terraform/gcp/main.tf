# GCP GKE root: VPC with private nodes behind Cloud NAT on a reserved
# static IP (Capella allowlist / on-prem firewall), Artifact Registry read
# by the node service account, a Standard (not Autopilot) regional cluster,
# then the chart. Autopilot is avoided because it rewrites the Couchbase
# requests/limits and bills per pod for an always-on StatefulSet.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    google     = { source = "hashicorp/google",     version = "~> 6.0" }
    helm       = { source = "hashicorp/helm",       version = "~> 3.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = ">= 2.35" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_client_config" "default" {}

resource "google_project_service" "apis" {
  for_each = toset(["container.googleapis.com", "artifactregistry.googleapis.com", "compute.googleapis.com"])
  service            = each.key
  disable_on_destroy = false
}

# ---- Network ---------------------------------------------------------------

resource "google_compute_network" "aom" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "nodes" {
  name                     = "${var.cluster_name}-nodes"
  network                  = google_compute_network.aom.id
  region                   = var.region
  ip_cidr_range            = "10.70.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.72.0.0/14"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.76.0.0/20"
  }
}

resource "google_compute_address" "nat" {
  name   = "${var.cluster_name}-nat"
  region = var.region
}

resource "google_compute_router" "aom" {
  name    = "${var.cluster_name}-router"
  region  = var.region
  network = google_compute_network.aom.id
}

resource "google_compute_router_nat" "aom" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.aom.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.nat.self_link]
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# ---- Registry --------------------------------------------------------------

resource "google_artifact_registry_repository" "aom" {
  location      = var.region
  repository_id = "aom"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.aom.repository_id}"
}

resource "google_service_account" "nodes" {
  account_id   = "${var.cluster_name}-nodes"
  display_name = "AOM GKE node pool"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset(["roles/artifactregistry.reader", "roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.nodes.email}"
}

# ---- Cluster ---------------------------------------------------------------

resource "google_container_cluster" "aom" {
  name       = var.cluster_name
  location   = var.region
  network    = google_compute_network.aom.id
  subnetwork = google_compute_subnetwork.nodes.id

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel { channel = "REGULAR" }
  depends_on = [google_compute_router_nat.aom]
}

resource "google_container_node_pool" "aom" {
  name       = "aom"
  cluster    = google_container_cluster.aom.id
  node_count = var.node_count

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = 80
    disk_type       = "pd-balanced"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config { mode = "GKE_METADATA" }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

provider "kubernetes" {
  host                   = "https://${google_container_cluster.aom.endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.aom.master_auth[0].cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = "https://${google_container_cluster.aom.endpoint}"
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(google_container_cluster.aom.master_auth[0].cluster_ca_certificate)
  }
}

module "capella" {
  count  = var.couchbase_mode == "capella" ? 1 : 0
  source = "../capella"

  capella_api_token = var.capella_api_token
  organization_id   = var.capella_organization_id
  project_id        = var.capella_project_id
  cluster_id        = var.capella_cluster_id
  egress_cidrs      = ["${google_compute_address.nat.address}/32"]
}

module "aom" {
  source = "../modules/aom-release"

  chart_path         = "${path.module}/../../helm/couchbase-agent-operations-manager"
  image_registry     = local.registry
  image_tag          = var.image_tag
  storage_class      = "standard-rwo" # GCE PD CSI default, WaitForFirstConsumer
  couchbase_password = var.couchbase_mode == "capella" ? module.capella[0].credential.password : var.couchbase_password
  couchbase_username = var.couchbase_mode == "capella" ? module.capella[0].credential.username : "Administrator"
  auth_secret_key    = var.auth_secret_key
  provider_api_keys  = var.provider_api_keys

  couchbase_mode     = var.couchbase_mode
  external_couchbase = var.couchbase_mode == "capella" ? {
    host   = module.capella[0].host
    bucket = module.capella[0].bucket
  } : var.external_couchbase

  monitoring = var.monitoring

  # External passthrough Network LB by default; for internal only:
  # ui_service_annotations = { "networking.gke.io/load-balancer-type" = "Internal" }

  depends_on = [google_container_node_pool.aom, google_project_iam_member.nodes]
}
