output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
output "registry"             { value = local.registry }
output "egress_ip"            { value = module.vpc.nat_public_ips }
output "dashboard_url"        { value = module.aom.dashboard_url }
output "grafana_port_forward" { value = module.aom.grafana_port_forward }
