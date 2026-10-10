terraform {
  required_providers {
    helm       = { source = "hashicorp/helm" }
    kubernetes = { source = "hashicorp/kubernetes" }
  }
}

locals {
  # kube-prometheus-stack's Prometheus only picks up ServiceMonitors/
  # PrometheusRules carrying release=<its release name> unless told
  # otherwise; when this module installs the stack, that name is known.
  monitoring_release_label = var.monitoring.install_stack ? "kube-prometheus-stack" : var.monitoring.release_label
}

resource "kubernetes_namespace_v1" "aom" {
  metadata { name = var.namespace }
}

# ---- Optional: Prometheus + Grafana ----------------------------------------

resource "kubernetes_namespace_v1" "monitoring" {
  count = var.monitoring.install_stack ? 1 : 0
  metadata { name = var.monitoring.namespace }
}

resource "helm_release" "kube_prometheus_stack" {
  count = var.monitoring.install_stack ? 1 : 0

  name       = "kube-prometheus-stack"
  namespace  = kubernetes_namespace_v1.monitoring[0].metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.monitoring.stack_chart_version != "" ? var.monitoring.stack_chart_version : null
  timeout    = 900
  wait       = true

  values = [yamlencode({
    prometheus = {
      prometheusSpec = {
        # Watch ServiceMonitors / rules in every namespace that carries the
        # release label, not only the stack's own namespace.
        serviceMonitorNamespaceSelector = {}
        ruleNamespaceSelector           = {}
        retention                       = "15d"
      }
    }
    grafana = {
      adminPassword = var.monitoring.grafana_admin_password != "" ? var.monitoring.grafana_admin_password : null
      sidecar = {
        dashboards = {
          enabled         = true
          searchNamespace = "ALL" # finds the AOM dashboard ConfigMap in agent-ops
        }
      }
    }
  })]
}

# ---- AOM -------------------------------------------------------------------

resource "helm_release" "aom" {
  name      = var.release_name
  namespace = kubernetes_namespace_v1.aom.metadata[0].name
  chart     = var.chart_path

  # couchbase-init runs as a post-install hook and index builds on a large
  # external collection can take minutes; bundled Couchbase EE needs ~2min
  # just to initialise.
  timeout = 900
  wait    = true

  values = [
    templatefile("${path.module}/values.tftpl", {
      image_registry           = var.image_registry
      image_tag                = var.image_tag
      storage_class            = var.storage_class
      couchbase_username       = var.couchbase_username
      couchbase_password       = var.couchbase_password
      auth_secret_key          = var.auth_secret_key
      couchbase_memory_limit   = var.couchbase_memory_limit
      couchbase_storage_size   = var.couchbase_storage_size
      ui_service_annotations   = var.ui_service_annotations
      keys                     = var.provider_api_keys
      couchbase_mode           = var.couchbase_mode
      ext                      = var.external_couchbase
      monitoring               = var.monitoring
      monitoring_release_label = local.monitoring_release_label
    })
  ]

  # The ServiceMonitor/PrometheusRule CRDs must exist before the chart
  # renders them.
  depends_on = [helm_release.kube_prometheus_stack]
}

data "kubernetes_service_v1" "ui" {
  metadata {
    name      = "${var.release_name}-ui"
    namespace = var.namespace
  }
  depends_on = [helm_release.aom]
}
