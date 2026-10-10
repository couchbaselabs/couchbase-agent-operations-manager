variable "chart_path" {
  description = "Path to helm/couchbase-agent-operations-manager in this repo"
  type        = string
}

variable "namespace" {
  type    = string
  default = "agent-ops"
}

variable "release_name" {
  description = "Keep it short: the chart truncates aom.fullname to 40 chars (StatefulSet label limit)"
  type        = string
  default     = "aom"
}

variable "image_registry" {
  description = "Registry prefix the three images were pushed under, e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/aom (maps to global.imageRegistry)"
  type        = string
}

variable "image_tag" {
  type = string
}

variable "storage_class" {
  description = "StorageClass for the Couchbase PVC (bundled mode) and the embedding-model cache PVC"
  type        = string
}

variable "couchbase_password" {
  description = "Bundled mode: the admin password the chart sets on its own Couchbase. External/Capella: the credential operations-manager runs with."
  type      = string
  sensitive = true
}

variable "couchbase_username" {
  type    = string
  default = "Administrator"
}

variable "auth_secret_key" {
  description = "Dashboard session signing key (operationsManager.auth.secretKey). Generate once with `openssl rand -hex 32` and never rotate casually - it also encrypts stored LDAP/SIEM secrets."
  type        = string
  sensitive   = true
}

variable "provider_api_keys" {
  description = "Optional LLM/embedding provider keys (operationsManager.providerApiKeys); blank = offline stub"
  type = object({
    anthropic  = optional(string, "")
    openai     = optional(string, "")
    gemini     = optional(string, "")
    databricks = optional(string, "")
    voyage     = optional(string, "")
    cohere     = optional(string, "")
    mistral    = optional(string, "")
    jina       = optional(string, "")
  })
  default   = {}
  sensitive = true
}

variable "couchbase_memory_limit" {
  description = "Bundled Couchbase pod limit. 6Gi is the validated default (3Gi OOMKills at boot); re-test before lowering."
  type        = string
  default     = "6Gi"
}

variable "couchbase_storage_size" {
  type    = string
  default = "50Gi"
}

variable "ui_service_annotations" {
  description = "Cloud-specific LoadBalancer annotations for the ui Service"
  type        = map(string)
  default     = {}
}

# ---- External Couchbase ----------------------------------------------------

variable "couchbase_mode" {
  description = <<-EOT
    bundled    - single-node Couchbase EE StatefulSet in the cluster (dev/eval)
    enterprise - an existing self-managed Couchbase Server EE cluster; the
                 chart's couchbase-init Job creates the bucket, scope,
                 collections and indexes on first boot
    capella    - Couchbase Capella; terraform/capella creates the bucket and
                 database credential, the init Job provisions inside the bucket
  EOT
  type        = string
  default     = "bundled"
  validation {
    condition     = contains(["bundled", "enterprise", "capella"], var.couchbase_mode)
    error_message = "couchbase_mode must be bundled, enterprise or capella."
  }
}

variable "external_couchbase" {
  description = "Ignored when couchbase_mode = bundled. Maps to operationsManager.couchbase.* and couchbase.external.* in the chart."
  type = object({
    host               = optional(string, "") # bare hostname: cb.example.internal / cb.xxxx.cloud.couchbase.com
    tls                = optional(bool, true)
    tls_ca_pem         = optional(string, "") # private CA bundle; blank for Capella / public CAs
    tls_insecure       = optional(bool, false)
    bucket             = optional(string, "agent_operations")
    scope              = optional(string, "agent_operations")
    provision          = optional(bool, true)
    provision_username = optional(string, "") # enterprise: an admin for bucket creation; blank = same as runtime user
    provision_password = optional(string, "")
  })
  default   = {}
  sensitive = true
}

# ---- Monitoring --------------------------------------------------------------

variable "monitoring" {
  description = <<-EOT
    Prometheus/Grafana integration. `install_stack` installs
    kube-prometheus-stack (Prometheus Operator, Prometheus, Alertmanager,
    Grafana with the dashboard sidecar) in `namespace` and turns on the
    chart's ServiceMonitor, PrometheusRule and Grafana dashboard ConfigMap.
    `service_monitor` alone is for a cluster that already runs the
    Prometheus Operator - set `release_label` to whatever its
    serviceMonitorSelector matches (kube-prometheus-stack: its release name).
  EOT
  type = object({
    install_stack          = optional(bool, false)
    namespace              = optional(string, "monitoring")
    stack_chart_version    = optional(string, "")
    grafana_admin_password = optional(string, "")
    service_monitor        = optional(bool, false)
    release_label          = optional(string, "")
    metrics_token          = optional(string, "")
  })
  default   = {}
  sensitive = true
}
