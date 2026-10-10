output "kubeconfig_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.aom.name} --region ${var.region} --project ${var.project_id}"
}
output "registry"             { value = local.registry }
output "egress_ip"            { value = google_compute_address.nat.address }
output "dashboard_url"        { value = module.aom.dashboard_url }
output "grafana_port_forward" { value = module.aom.grafana_port_forward }
