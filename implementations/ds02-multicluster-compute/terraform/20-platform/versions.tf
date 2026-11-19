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
      Purpose   = "design-02-multicluster-compute"
      Teardown  = "delete-after-use"
    }
  }
}

data "aws_eks_cluster" "c" {
  for_each = toset(local.cluster_keys)
  name     = "${var.project}-${each.key}"
}

locals {
  cluster_keys = ["hub", "cpu-a", "cpu-b", "gpu"]
  eks = {
    for k in local.cluster_keys : k => {
      name     = "${var.project}-${k}"
      endpoint = data.aws_eks_cluster.c[k].endpoint
      ca_b64   = data.aws_eks_cluster.c[k].certificate_authority[0].data
      exec     = ["eks", "get-token", "--cluster-name", "${var.project}-${k}", "--region", var.region]
    }
  }
}

# One provider set per cluster. Terraform cannot generate provider blocks in a loop, so they are written out.
provider "kubernetes" {
  alias                  = "hub"
  host                   = local.eks["hub"].endpoint
  cluster_ca_certificate = base64decode(local.eks["hub"].ca_b64)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["hub"].exec
  }
}
provider "kubectl" {
  alias                  = "hub"
  host                   = local.eks["hub"].endpoint
  cluster_ca_certificate = base64decode(local.eks["hub"].ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["hub"].exec
  }
}
provider "helm" {
  alias = "hub"
  kubernetes = {
    host                   = local.eks["hub"].endpoint
    cluster_ca_certificate = base64decode(local.eks["hub"].ca_b64)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.eks["hub"].exec
    }
  }
}

provider "kubernetes" {
  alias                  = "cpu_a"
  host                   = local.eks["cpu-a"].endpoint
  cluster_ca_certificate = base64decode(local.eks["cpu-a"].ca_b64)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["cpu-a"].exec
  }
}
provider "kubectl" {
  alias                  = "cpu_a"
  host                   = local.eks["cpu-a"].endpoint
  cluster_ca_certificate = base64decode(local.eks["cpu-a"].ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["cpu-a"].exec
  }
}
provider "helm" {
  alias = "cpu_a"
  kubernetes = {
    host                   = local.eks["cpu-a"].endpoint
    cluster_ca_certificate = base64decode(local.eks["cpu-a"].ca_b64)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.eks["cpu-a"].exec
    }
  }
}

provider "kubernetes" {
  alias                  = "cpu_b"
  host                   = local.eks["cpu-b"].endpoint
  cluster_ca_certificate = base64decode(local.eks["cpu-b"].ca_b64)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["cpu-b"].exec
  }
}
provider "kubectl" {
  alias                  = "cpu_b"
  host                   = local.eks["cpu-b"].endpoint
  cluster_ca_certificate = base64decode(local.eks["cpu-b"].ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["cpu-b"].exec
  }
}
provider "helm" {
  alias = "cpu_b"
  kubernetes = {
    host                   = local.eks["cpu-b"].endpoint
    cluster_ca_certificate = base64decode(local.eks["cpu-b"].ca_b64)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.eks["cpu-b"].exec
    }
  }
}

provider "kubernetes" {
  alias                  = "gpu"
  host                   = local.eks["gpu"].endpoint
  cluster_ca_certificate = base64decode(local.eks["gpu"].ca_b64)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["gpu"].exec
  }
}
provider "kubectl" {
  alias                  = "gpu"
  host                   = local.eks["gpu"].endpoint
  cluster_ca_certificate = base64decode(local.eks["gpu"].ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = local.eks["gpu"].exec
  }
}
provider "helm" {
  alias = "gpu"
  kubernetes = {
    host                   = local.eks["gpu"].endpoint
    cluster_ca_certificate = base64decode(local.eks["gpu"].ca_b64)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = local.eks["gpu"].exec
    }
  }
}
