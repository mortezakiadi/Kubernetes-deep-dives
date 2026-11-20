variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "project" {
  description = "Must match the cluster layer."
  type        = string
  default     = "ds03-trainserve"
}
# Every version is pinned. The gateway pieces are the combination the vLLM production-stack guide documents.
variable "versions" {
  type = map(string)
  default = {
    kueue        = "0.20.7"
    trainer      = "2.4.0"
    lws          = "1.0.0"
    agentgateway = "v1.4.1"
    llmd_router  = "v0.9.0"
    keda         = "2.21.1"
    prometheus   = "29.47.2"
    dcgm         = "4.9.0"
    vllm         = "v0.34.0"
    pytorch      = "2.7.1-cuda12.8-cudnn9-runtime"
  }
}
