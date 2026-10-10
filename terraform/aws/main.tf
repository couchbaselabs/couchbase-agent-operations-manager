# AWS EKS root. Builds VPC + EKS (terraform-aws-modules v21: `name`,
# `kubernetes_version`, AWS provider >= 6), ECR repos for the three images,
# a gp3 default StorageClass, then installs the chart via ../modules/aom-release.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    aws        = { source = "hashicorp/aws",        version = "~> 6.0" }
    helm       = { source = "hashicorp/helm",       version = "~> 3.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = ">= 2.35" }
  }
}

provider "aws" { region = var.region }

data "aws_availability_zones" "available" {}
data "aws_caller_identity" "current" {}

locals {
  azs      = slice(data.aws_availability_zones.available.names, 0, 3)
  registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/aom"
}

# ---- Network ---------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = "${var.cluster_name}-vpc"
  cidr = "10.40.0.0/16"
  azs  = local.azs

  private_subnets = ["10.40.0.0/20", "10.40.16.0/20", "10.40.32.0/20"]
  public_subnets  = ["10.40.100.0/24", "10.40.101.0/24", "10.40.102.0/24"]

  # One NAT gateway = one static egress IP, which is what a Capella
  # allowlist or an on-prem firewall rule needs.
  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# ---- Registry --------------------------------------------------------------

resource "aws_ecr_repository" "aom" {
  for_each             = toset(["couchbase-aom-operations-manager", "couchbase-aom-ui", "couchbase-aom-sample-mcp-servers"])
  name                 = "aom/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

# ---- Cluster ---------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = { before_compute = true }
    eks-pod-identity-agent = { before_compute = true }
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = {
    aom = {
      instance_types = [var.node_instance_type]
      ami_type       = "AL2023_x86_64_STANDARD"
      min_size       = var.node_count
      max_size       = var.node_count + 2
      desired_size   = var.node_count
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs         = { volume_size = 80, volume_type = "gp3" }
        }
      }
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name = "${var.cluster_name}-ebs-csi"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# ---- Providers wired to the new cluster -------------------------------------

data "aws_eks_cluster_auth" "this" { name = module.eks.cluster_name }

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}

resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name        = "gp3"
    annotations = { "storageclass.kubernetes.io/is-default-class" = "true" }
  }
  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  parameters             = { type = "gp3", encrypted = "true" }
  depends_on             = [module.eks]
}

# ---- Capella (only when couchbase_mode = capella) --------------------------

module "capella" {
  count  = var.couchbase_mode == "capella" ? 1 : 0
  source = "../capella"

  capella_api_token = var.capella_api_token
  organization_id   = var.capella_organization_id
  project_id        = var.capella_project_id
  cluster_id        = var.capella_cluster_id
  egress_cidrs      = [for ip in module.vpc.nat_public_ips : "${ip}/32"]
}

# ---- AOM -------------------------------------------------------------------

module "aom" {
  source = "../modules/aom-release"

  chart_path         = "${path.module}/../../helm/couchbase-agent-operations-manager"
  image_registry     = local.registry
  image_tag          = var.image_tag
  storage_class      = kubernetes_storage_class_v1.gp3.metadata[0].name
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

  ui_service_annotations = {
    "service.beta.kubernetes.io/aws-load-balancer-type"   = "external"
    "service.beta.kubernetes.io/aws-load-balancer-scheme" = "internet-facing"
  }

  depends_on = [module.eks, aws_ecr_repository.aom]
}
