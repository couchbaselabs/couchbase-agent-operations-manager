output "dashboard_url" {
  value = try(
    "https://${coalesce(
      data.kubernetes_service_v1.ui.status[0].load_balancer[0].ingress[0].hostname,
      data.kubernetes_service_v1.ui.status[0].load_balancer[0].ingress[0].ip
    )}",
    "pending - kubectl -n ${var.namespace} get svc ${var.release_name}-ui"
  )
}

output "grafana_port_forward" {
  value = var.monitoring.install_stack ? "kubectl -n ${var.monitoring.namespace} port-forward svc/kube-prometheus-stack-grafana 3000:80" : null
}

output "namespace" {
  value = var.namespace
}
