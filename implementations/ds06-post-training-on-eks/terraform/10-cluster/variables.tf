variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  description = "Design 05 uses us-east-1 and eu-west-1; this build stays out of both."
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Name prefix and the Project tag on every resource, used by the teardown check. Must start with ds06-."
  type        = string
  default     = "ds06-posttrain"
  validation {
    condition     = startswith(var.project, "ds06-")
    error_message = "Every name in this build starts with ds06-."
  }
}

variable "kubernetes_version" {
  type    = string
  default = "1.37"
}

variable "admin_cidr" {
  description = "Public IP (as a /32) allowed to reach the cluster API endpoint. up.sh fills this in."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.61.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["us-east-2a", "us-east-2b", "us-east-2c"]
}

# The GPU pool, in order of preference. Each type has one NVIDIA GPU and 8 vCPU: g6.2xlarge (L4, 24 GiB, 32 GiB RAM),
# g6e.2xlarge (L40S, 48 GiB, 64 GiB RAM), g5.2xlarge (A10G, 24 GiB, 32 GiB RAM). All support bfloat16, which the
# training code needs. When this build was made, us-east-2 refused g5.2xlarge in every zone and g6.2xlarge in two of
# three, so the list is a list on purpose. Each run records the GPU it got.
variable "gpu_instance_types" {
  type    = list(string)
  default = ["g6.2xlarge", "g6e.2xlarge", "g5.2xlarge"]
}

variable "gpu_nodes" {
  description = "GPU nodes in the pool. Two is the smallest pool in which a rollout engine and a trainer each get a GPU."
  type        = number
  default     = 2
}

# Pinned accelerated AMI release (NVIDIA driver, CUDA user space, containerd). Read from
# /aws/service/eks/optimized-ami/1.37/amazon-linux-2023/x86_64/nvidia/recommended/release_version when this build was written.
variable "gpu_ami_release" {
  type    = string
  default = "1.37.1-20261119"
}
