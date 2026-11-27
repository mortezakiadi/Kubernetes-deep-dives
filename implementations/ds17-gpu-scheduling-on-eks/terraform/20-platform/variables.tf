variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-2"
}

variable "project" {
  description = "Must match the cluster layer."
  type        = string
  default     = "ds17-sched"
}

variable "versions" {
  type = map(string)
  default = {
    kueue         = "0.20.8"
    device_plugin = "0.20.2"
    dcgm_exporter = "4.9.0"
  }
}

variable "python_image" {
  type    = string
  default = "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
}

# The second scheduler: the same minor version as the cluster, pinned by digest.
variable "scheduler_image" {
  type    = string
  default = "registry.k8s.io/kube-scheduler:v1.37.1@sha256:b6e2474a6c20309f0df1e7337364281fda95c6a0722259ba7f032a1e177d3f29"
}

# Real GPU load for S02: the CUDA n-body sample, pinned by digest.
variable "cuda_sample_image" {
  type    = string
  default = "nvcr.io/nvidia/k8s/cuda-sample:nbody@sha256:59261e419d6d48a772aad5bb213f9f1588fcdb042b115ceb7166c89a51f03363"
}
