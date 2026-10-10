variable "project_id"        { type = string }
variable "region"            { type = string  default = "us-central1" }
variable "cluster_name"      { type = string  default = "aom" }
variable "node_machine_type" { type = string  default = "e2-standard-4" } # 4 vCPU / 16 GiB
variable "node_count"        { type = number  default = 1 }               # per zone x 3 zones (regional cluster)
# Variables every cloud root shares (copied verbatim into each root's
# variables.tf by design - Terraform has no include; keep them identical).
variable "image_tag"          { type = string }
variable "couchbase_password" { type = string  sensitive = true }
variable "auth_secret_key"    { type = string  sensitive = true }
variable "provider_api_keys"  { type = map(string) default = {} sensitive = true }

variable "couchbase_mode" {
  description = "bundled | enterprise | capella - see modules/aom-release"
  type        = string
  default     = "bundled"
}
variable "external_couchbase" {
  description = "Settings for couchbase_mode = enterprise; ignored otherwise (capella fills these from module.capella)"
  type        = any
  default     = {}
  sensitive   = true
}
variable "monitoring" {
  description = "See modules/aom-release variable `monitoring`"
  type        = any
  default     = {}
  sensitive   = true
}

# Capella (couchbase_mode = capella only)
variable "capella_api_token"       { type = string  default = "" sensitive = true }
variable "capella_organization_id" { type = string  default = "" }
variable "capella_project_id"      { type = string  default = "" }
variable "capella_cluster_id"      { type = string  default = "" }
