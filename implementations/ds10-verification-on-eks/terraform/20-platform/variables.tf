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
  default     = "ds10-verify"
}

variable "data_dir" {
  description = "Where up.sh generated the datasets (datasets/make_data.py)."
  type        = string
  default     = "../../.build/data"
}

variable "wheels_dir" {
  description = "Where up.sh downloaded the pinned SymPy wheels (served to the verifier pods by the store)."
  type        = string
  default     = "../../.build/wheels"
}

variable "versions" {
  type    = map(string)
  default = { device_plugin = "0.20.2" }
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

# Two judges of different model families, each pinned to a commit (both Apache-2.0).
variable "judges" {
  type = map(object({ repo = string, revision = string }))
  default = {
    judge-a = { repo = "Qwen/Qwen2.5-7B-Instruct", revision = "a09a35458c702b33eeacc393d103063234e8bc28" }
    judge-b = { repo = "mistralai/Mistral-7B-Instruct-v0.3", revision = "c170c708c41dac9275d15a8fff4eca08d52bab71" }
  }
}

# The policy of the red-team RL runs (Apache-2.0), pinned to a commit.
variable "policy" {
  type = map(string)
  default = {
    repo     = "Qwen/Qwen2.5-0.5B-Instruct"
    revision = "7ae557604adf67be50417f59c2c2f167def9a775"
  }
}

# Pinned verifier dependencies, installed from the store with --require-hashes (the verifier pods have no internet).
variable "wheels" {
  type = map(string)
  default = {
    "sympy-1.14.0-py3-none-any.whl" = "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5"
    "mpmath-1.3.0-py3-none-any.whl" = "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c"
  }
}
