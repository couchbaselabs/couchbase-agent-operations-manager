# Couchbase Capella: the parts only the Capella management API can do -
# bucket, database credential, IP allowlist. Everything inside the bucket
# (scope, collections, indexes, the settings::provisioned marker) is done by
# the chart's couchbase-init Job on first boot, so couchbase-init/init.sh
# stays the one definition of the appliance's schema.
#
# Resource attribute names follow the couchbase-cloud/couchbase-capella
# provider's examples; the provider has renamed attributes between minors,
# so run `terraform validate` against the pinned version before trusting
# this verbatim.
terraform {
  required_providers {
    couchbase-capella = { source = "couchbase-cloud/couchbase-capella", version = "~> 1.0" }
  }
}

variable "capella_api_token" { type = string  sensitive = true }
variable "organization_id"   { type = string }
variable "project_id"        { type = string }
variable "cluster_id"        { type = string }        # existing cluster with Data, Index, Query AND Search
variable "egress_cidrs"      { type = list(string) }  # the Kubernetes cluster's NAT / outbound IPs
variable "bucket_name"       { type = string  default = "agent_operations" }
variable "bucket_memory_mb"  { type = number  default = 1024 }

provider "couchbase-capella" {
  authentication_token = var.capella_api_token
}

resource "couchbase-capella_bucket" "aom" {
  organization_id            = var.organization_id
  project_id                 = var.project_id
  cluster_id                 = var.cluster_id
  name                       = var.bucket_name
  type                       = "couchbase"
  storage_backend            = "couchstore"
  memory_allocation_in_mb    = var.bucket_memory_mb
  bucket_conflict_resolution = "seqno"
  durability_level           = "none"
  replicas                   = 1
  flush                      = false
  time_to_live_in_seconds    = 0
  # Full eviction: the appliance writes millions of small short-lived
  # documents (audit entries, spans, cache events) - see init.sh's
  # CB_EVICTION_POLICY comment.
  eviction_policy = "fullEviction"
}

# One credential for both the init Job and operations-manager. Read/Write
# on the bucket is what grants scope/collection creation, index management
# and Search index administration on Capella.
resource "couchbase-capella_database_credential" "aom" {
  organization_id = var.organization_id
  project_id      = var.project_id
  cluster_id      = var.cluster_id
  name            = "aom-operations-manager"

  access = [{
    privileges = ["data_reader", "data_writer"]
    resources  = { buckets = [{ name = couchbase-capella_bucket.aom.name, scopes = [] }] }
  }]
}

resource "couchbase-capella_allowlist" "aom" {
  for_each = toset(var.egress_cidrs)

  organization_id = var.organization_id
  project_id      = var.project_id
  cluster_id      = var.cluster_id
  cidr            = each.key
  comment         = "AOM Kubernetes egress"
}

data "couchbase-capella_cluster" "this" {
  organization_id = var.organization_id
  project_id      = var.project_id
  cluster_id      = var.cluster_id
}

output "host" {
  description = "Bare hostname (cb.xxxx.cloud.couchbase.com) - the chart builds couchbases:// and the REST URLs from it"
  value       = trimprefix(trimprefix(data.couchbase-capella_cluster.this.connection_string, "couchbases://"), "couchbase://")
}
output "bucket" { value = couchbase-capella_bucket.aom.name }
output "credential" {
  value = {
    username = couchbase-capella_database_credential.aom.name
    password = couchbase-capella_database_credential.aom.password
  }
  sensitive = true
}
