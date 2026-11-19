variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  description = "Must match the cluster layer. Used for tags and names."
  type        = string
  default     = "ds01-sandboxing"
}

variable "cluster_name" {
  description = "Name of the cluster created by terraform/10-cluster."
  type        = string
  default     = "ds01-sandboxing"
}

variable "gateway_ip" {
  description = "Fixed ClusterIP for the egress gateway. Sandboxes have no DNS, so they reach the proxy by address."
  type        = string
  default     = "172.20.200.10"
}

variable "python_pool_replicas" {
  type    = number
  default = 2
}

variable "compute_pool_replicas" {
  type    = number
  default = 1
}

variable "controller_version" {
  description = "agent-sandbox release. The SHA-256 below pins the manifest."
  type        = string
  default     = "v1.0.10"
}

variable "controller_manifest_sha256" {
  type    = string
  default = "7cc1eca2fb45844b0ec0c35fc975340dc01af2994301ffa8bf05db1ad1e397ee"
}
