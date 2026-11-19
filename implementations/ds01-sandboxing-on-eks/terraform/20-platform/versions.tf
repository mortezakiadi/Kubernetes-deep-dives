terraform {
  required_version = ">= 1.9"

  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
    kubectl    = { source = "alekc/kubectl", version = "~> 2.1" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    tls        = { source = "hashicorp/tls", version = "~> 4.0" }
    http       = { source = "hashicorp/http", version = "~> 3.4" }
    time       = { source = "hashicorp/time", version = "~> 0.12" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Purpose   = "design-01-sandboxing"
      Teardown  = "delete-after-use"
    }
  }
}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

locals {
  eks_host = data.aws_eks_cluster.this.endpoint
  eks_ca   = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
  eks_exec = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
}

provider "kubernetes" {
  host                   = local.eks_host
  cluster_ca_certificate = local.eks_ca
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks_exec
  }
}

provider "kubectl" {
  host                   = local.eks_host
  cluster_ca_certificate = local.eks_ca
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks_exec
  }
}
