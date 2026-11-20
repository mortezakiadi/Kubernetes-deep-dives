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
  default     = "ds04-agentstate"
}
variable "db_host" {
  description = "The PostgreSQL endpoint. up.sh fills this in from the cluster layer."
  type        = string
}
variable "db_password" {
  description = "The database password. up.sh fills this in; it lives only in git-ignored files and in the cluster's Secrets."
  type        = string
  sensitive   = true
}
variable "agent_image" {
  description = "The agent image in ECR, with its tag. up.sh builds and pushes it, then fills this in."
  type        = string
}
variable "versions" {
  type = map(string)
  default = {
    temporal_chart = "1.8.0"
    keda           = "2.21.1"
    prometheus     = "29.47.2"
  }
}
