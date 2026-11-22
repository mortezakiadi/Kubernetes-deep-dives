terraform {
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    kubectl = { source = "alekc/kubectl", version = "~> 2.1" }
    helm    = { source = "hashicorp/helm", version = "~> 3.0" }
    http    = { source = "hashicorp/http", version = "~> 3.4" }
  }
}
variable "account_id" { type = string }
variable "project" {
  type    = string
  default = "ds07-scaling-rl"
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "versions" {
  type = object({ device_plugin = string, jobset = string, lws = string, keda = string })
  default = {
    device_plugin = "0.20.2"
    jobset        = "v0.13.1"
    lws           = "v1.0.0"
    keda          = "2.21.1"
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
data "terraform_remote_state" "cluster" {
  backend = "local"
  config  = { path = "${path.module}/../10-cluster/terraform.tfstate" }
}
locals {
  cluster = data.terraform_remote_state.cluster.outputs.cluster
}
provider "kubectl" {
  host                   = local.cluster.endpoint
  cluster_ca_certificate = base64decode(local.cluster.ca_b64)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.cluster.name, "--region", var.region]
  }
}
provider "helm" {
  kubernetes = {
    host                   = local.cluster.endpoint
    cluster_ca_certificate = base64decode(local.cluster.ca_b64)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.cluster.name, "--region", var.region]
    }
  }
}
