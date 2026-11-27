variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  description = "Design 21's build uses us-east-1; this build stays out of it."
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Name prefix and the Project tag on every resource, used by the teardown check. Must start with ds17-."
  type        = string
  default     = "ds17-sched"
  validation {
    condition     = startswith(var.project, "ds17-")
    error_message = "Every name in this build starts with ds17-."
  }
}

variable "kubernetes_version" {
  type    = string
  default = "1.37"
}

variable "admin_ips" {
  description = "Comma-separated public IPs allowed to reach the cluster API endpoint, each as a /32. up.sh fills this in with every egress address it observes, because some networks rotate through a small pool."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.86.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["us-east-2a", "us-east-2b", "us-east-2c"]
}

# The GPU pool, in order of preference. Every type has one NVIDIA GPU that supports bfloat16: g6e (L40S, 48 GB),
# g6 (L4, 24 GB), g5 (A10G, 24 GB). When this build was made, EC2 in us-east-2 refused g6.2xlarge and g6e.2xlarge in
# most zones while the xlarge sizes had capacity, so the list mixes sizes on purpose. Pod requests fit the smallest
# (4 vCPU, 16 GiB). Each run records which GPU it got.
variable "gpu_instance_types" {
  type    = list(string)
  default = ["g6e.xlarge", "g6.2xlarge", "g6e.2xlarge", "g5.xlarge", "g5.2xlarge", "g6.xlarge"]
}

variable "gpu_nodes" {
  description = "Real GPU nodes: two, so one busy and one idle pod each hold a whole GPU (S02)."
  type        = number
  default     = 2
}

# Pinned accelerated AMI release (NVIDIA driver, CUDA user space, containerd). Read from
# /aws/service/eks/optimized-ami/1.37/amazon-linux-2023/x86_64/nvidia/recommended/release_version when this build was written.
variable "gpu_ami_release" {
  type    = string
  default = "1.37.1-20261119"
}
