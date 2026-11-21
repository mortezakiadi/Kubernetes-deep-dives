terraform {
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    kubectl = { source = "alekc/kubectl", version = "~> 2.1" }
  }
}
locals {
  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    Purpose   = "design-05-networking"
    Teardown  = "delete-after-use"
  }
}
provider "aws" {
  region              = var.regions.main
  allowed_account_ids = [var.account_id]
  default_tags { tags = local.tags }
}
provider "aws" {
  alias               = "second"
  region              = var.regions.second
  allowed_account_ids = [var.account_id]
  default_tags { tags = local.tags }
}
provider "kubectl" {
  alias                  = "main"
  host                   = module.cluster_main.endpoint
  cluster_ca_certificate = base64decode(module.cluster_main.ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--region", var.regions.main, "--cluster-name", module.cluster_main.name]
  }
}
provider "kubectl" {
  alias                  = "second"
  host                   = module.cluster_second.endpoint
  cluster_ca_certificate = base64decode(module.cluster_second.ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--region", var.regions.second, "--cluster-name", module.cluster_second.name]
  }
}
