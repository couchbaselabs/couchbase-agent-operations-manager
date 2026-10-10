output "kubeconfig_command" {
  value = "az aks get-credentials --resource-group ${azurerm_resource_group.aom.name} --name ${azurerm_kubernetes_cluster.aom.name}"
}
output "registry"             { value = local.registry }
output "egress_ip"            { value = azurerm_public_ip.egress.ip_address }
output "dashboard_url"        { value = module.aom.dashboard_url }
output "grafana_port_forward" { value = module.aom.grafana_port_forward }
