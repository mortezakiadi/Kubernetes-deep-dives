terraform {
  required_version = ">= 1.9"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.35" }
    kubectl    = { source = "alekc/kubectl", version = "~> 2.1" }
    helm       = { source = "hashicorp/helm", version = "~> 3.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
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
      Purpose   = "design-10-verification"
      Teardown  = "delete-after-use"
    }
  }
}

data "aws_eks_cluster" "c" {
  name = var.project
}

locals {
  endpoint = data.aws_eks_cluster.c.endpoint
  ca       = base64decode(data.aws_eks_cluster.c.certificate_authority[0].data)
  exec     = ["eks", "get-token", "--cluster-name", var.project, "--region", var.region]
}

provider "kubernetes" {
  host                   = local.endpoint
  cluster_ca_certificate = local.ca
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.exec
  }
}

provider "kubectl" {
  host                   = local.endpoint
  cluster_ca_certificate = local.ca
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.exec
  }
}

provider "helm" {
  kubernetes = {
    host                   = local.endpoint
    cluster_ca_certificate = local.ca
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.exec
    }
  }
}
