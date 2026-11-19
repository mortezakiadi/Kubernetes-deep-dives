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
  default     = "ds02-fleet"
}

variable "kubernetes_version" {
  description = "Every cluster in the fleet runs the same minor version (Design 02: keep the fleet within one minor)."
  type        = string
  default     = "1.37"
}

variable "admin_cidr" {
  description = "Public IP (as a /32) allowed to reach the cluster API endpoints. up.sh fills this in."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

# One entry per cluster. "role" is the label Design 02 uses to decide which add-ons a cluster gets.
variable "clusters" {
  type = map(object({ role = string }))
  default = {
    hub   = { role = "manager" }
    cpu-a = { role = "cpu-worker" }
    cpu-b = { role = "cpu-worker" }
    gpu   = { role = "gpu-worker" }
  }
}
