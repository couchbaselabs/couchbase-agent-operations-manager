# Azure AKS root: VNet, ACR (kubelet identity gets AcrPull - no pull
# secret), AKS with a system pool + a dedicated `aom` user pool, a static
# egress IP (for Capella allowlists / on-prem firewalls), then the chart.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    azurerm    = { source = "hashicorp/azurerm",    version = "~> 4.0" }
    helm       = { source = "hashicorp/helm",       version = "~> 3.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = ">= 2.35" }
  }
}

provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "aom" {
  name     = var.resource_group
  location = var.location
}

resource "azurerm_virtual_network" "aom" {
  name                = "${var.cluster_name}-vnet"
  location            = azurerm_resource_group.aom.location
  resource_group_name = azurerm_resource_group.aom.name
  address_space       = ["10.50.0.0/16"]
}

resource "azurerm_subnet" "nodes" {
  name                 = "nodes"
  resource_group_name  = azurerm_resource_group.aom.name
  virtual_network_name = azurerm_virtual_network.aom.name
  address_prefixes     = ["10.50.0.0/20"]
}

resource "azurerm_public_ip" "egress" {
  name                = "${var.cluster_name}-egress"
  location            = azurerm_resource_group.aom.location
  resource_group_name = azurerm_resource_group.aom.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_container_registry" "aom" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.aom.name
  location            = azurerm_resource_group.aom.location
  sku                 = "Standard"
  admin_enabled       = false
}

resource "azurerm_kubernetes_cluster" "aom" {
  name                = var.cluster_name
  location            = azurerm_resource_group.aom.location
  resource_group_name = azurerm_resource_group.aom.name
  dns_prefix          = var.cluster_name
  kubernetes_version  = var.kubernetes_version

  default_node_pool {
    name                         = "system"
    vm_size                      = "Standard_D2s_v5"
    node_count                   = 1
    vnet_subnet_id               = azurerm_subnet.nodes.id
    only_critical_addons_enabled = true
  }

  identity { type = "SystemAssigned" }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
    service_cidr   = "10.60.0.0/16"
    dns_service_ip = "10.60.0.10"
    outbound_type  = "loadBalancer"
    load_balancer_profile {
      outbound_ip_address_ids = [azurerm_public_ip.egress.id]
    }
  }

  oidc_issuer_enabled       = true
  workload_identity_enabled = true
}

resource "azurerm_kubernetes_cluster_node_pool" "aom" {
  name                  = "aom"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aom.id
  vm_size               = var.node_vm_size
  node_count            = var.node_count
  vnet_subnet_id        = azurerm_subnet.nodes.id
  os_disk_size_gb       = 80
  mode                  = "User"
}

resource "azurerm_role_assignment" "acr_pull" {
  principal_id                     = azurerm_kubernetes_cluster.aom.kubelet_identity[0].object_id
  role_definition_name             = "AcrPull"
  scope                            = azurerm_container_registry.aom.id
  skip_service_principal_aad_check = true
}

locals {
  kc       = azurerm_kubernetes_cluster.aom.kube_config[0]
  registry = "${azurerm_container_registry.aom.login_server}/aom"
}

provider "kubernetes" {
  host                   = local.kc.host
  client_certificate     = base64decode(local.kc.client_certificate)
  client_key             = base64decode(local.kc.client_key)
  cluster_ca_certificate = base64decode(local.kc.cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = local.kc.host
    client_certificate     = base64decode(local.kc.client_certificate)
    client_key             = base64decode(local.kc.client_key)
    cluster_ca_certificate = base64decode(local.kc.cluster_ca_certificate)
  }
}

module "capella" {
  count  = var.couchbase_mode == "capella" ? 1 : 0
  source = "../capella"

  capella_api_token = var.capella_api_token
  organization_id   = var.capella_organization_id
  project_id        = var.capella_project_id
  cluster_id        = var.capella_cluster_id
  egress_cidrs      = ["${azurerm_public_ip.egress.ip_address}/32"]
}

module "aom" {
  source = "../modules/aom-release"

  chart_path         = "${path.module}/../../helm/couchbase-agent-operations-manager"
  image_registry     = local.registry
  image_tag          = var.image_tag
  storage_class      = "managed-csi" # built-in Azure Disk CSI, WaitForFirstConsumer
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

  # Public Standard LB by default; for internal only:
  # ui_service_annotations = { "service.beta.kubernetes.io/azure-load-balancer-internal" = "true" }

  depends_on = [azurerm_kubernetes_cluster_node_pool.aom, azurerm_role_assignment.acr_pull]
}
