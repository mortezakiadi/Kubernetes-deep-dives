variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  description = "Name prefix and the Project tag on every resource, used by the teardown check."
  type        = string
  default     = "ds01-sandboxing"
}

variable "kubernetes_version" {
  description = "Nitro Sandboxes need Kubernetes 1.36 or later."
  type        = string
  default     = "1.37"
}

variable "admin_cidr" {
  description = "Public IP (as a /32) allowed to reach the cluster API endpoint. up.sh fills this in."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}
