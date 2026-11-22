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
  default     = "ds06-posttrain"
}

variable "datasets_dir" {
  description = "Where up.sh generated the datasets (datasets/make_datasets.py)."
  type        = string
  default     = "../../.build/datasets"
}

# Every version is pinned.
variable "versions" {
  type = map(string)
  default = {
    kueue         = "0.20.7"
    jobset        = "0.13.1"
    device_plugin = "0.20.2"
  }
}

variable "python_image" {
  type    = string
  default = "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
}
