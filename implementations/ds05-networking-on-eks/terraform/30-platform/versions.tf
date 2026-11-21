terraform {
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    kubectl = { source = "alekc/kubectl", version = "~> 2.1" }
    helm    = { source = "hashicorp/helm", version = "~> 3.0" }
    http    = { source = "hashicorp/http", version = "~> 3.4" }
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
data "aws_eks_cluster" "main" { name = local.clusters.main.name }
data "aws_eks_cluster" "second" {
  provider = aws.second
  name     = local.clusters.second.name
}
provider "kubectl" {
  alias                  = "main"
  host                   = data.aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.clusters.main.name, "--region", var.regions.main]
  }
}
provider "kubectl" {
  alias                  = "second"
  host                   = data.aws_eks_cluster.second.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.second.certificate_authority[0].data)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.clusters.second.name, "--region", var.regions.second]
  }
}
provider "helm" {
  alias = "main"
  kubernetes = {
    host                   = data.aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.clusters.main.name, "--region", var.regions.main]
    }
  }
}
provider "helm" {
  alias = "second"
  kubernetes = {
    host                   = data.aws_eks_cluster.second.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.second.certificate_authority[0].data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.clusters.second.name, "--region", var.regions.second]
    }
  }
}
