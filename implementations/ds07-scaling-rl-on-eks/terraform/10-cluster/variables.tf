variable "account_id" { type = string }
variable "project" {
  type    = string
  default = "ds07-scaling-rl"
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "azs" {
  description = "Three zones. The GPU nodes all go to the first, so weight transfer and rollouts stay inside one zone."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}
variable "kubernetes_version" {
  type    = string
  default = "1.37"
}
variable "admin_cidr" {
  description = "Public IP (as a /32) allowed to reach the cluster API endpoint. up.sh and scripts/update-my-ip.sh fill this in."
  type        = string
}
variable "trainer_instance_types" {
  description = "In order of preference. A node that is lost may not be replaceable by the same type (S09 found the zone out of capacity for ninety minutes), so the pool lists other single-GPU types."
  type        = list(string)
  default     = ["g5.2xlarge", "g5.4xlarge", "g6.2xlarge", "g6e.2xlarge"]
}
variable "rollout_instance_type" {
  type    = string
  default = "g5.2xlarge"
}
variable "rollout_nodes" {
  type    = number
  default = 2
}
variable "env_nodes" {
  description = "CPU nodes for the sandbox (environment) tier."
  type        = number
  default     = 2
}
