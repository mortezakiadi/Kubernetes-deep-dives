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
  default     = "ds08-longhorizon"
}

variable "tasks_dir" {
  description = "Where up.sh generated the task bank (tasks/make_tasks.py)."
  type        = string
  default     = "../../.build/tasks"
}

variable "versions" {
  type = map(string)
  default = {
    kueue         = "0.20.7"
    device_plugin = "0.20.2"
  }
}

variable "python_image" {
  type    = string
  default = "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
}

variable "vllm_image" {
  description = "vLLM 0.33.0 release image, amd64, pinned by digest."
  type        = string
  default     = "docker.io/vllm/vllm-openai@sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3"
}

# The policy: the model the rollout engine serves and the trainer loads. Pinned to a commit.
variable "policy" {
  type = map(string)
  default = {
    repo     = "Qwen/Qwen2.5-7B-Instruct"
    revision = "a09a35458c702b33eeacc393d103063234e8bc28"
  }
}
