variable "account_id" { type = string }
variable "project" {
  type    = string
  default = "ds11-multi-task"
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "azs" {
  description = "Three zones. Every experiment is one pod on one GPU, so the GPU pool may use all of them: more zones, more chances of finding capacity."
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
variable "research_instance_types" {
  description = "In order of preference. Each experiment is one pod on one 24 GB-or-larger GPU, so the pool lists every single-GPU type that fits (A10G, L4, L40S): GPU capacity on demand is volatile, and any of them will do."
  type        = list(string)
  default     = ["g6e.2xlarge", "g5.2xlarge", "g6.2xlarge", "g5.4xlarge"]
}
variable "research_nodes" {
  description = "How many GPU nodes to create the pool with. It starts empty: a pool whose first node cannot be launched (no capacity in the zone) fails to create, so the tests scale it up afterwards and try again as often as needed (max 4)."
  type        = number
  default     = 0
}
