variable "account_id" { type = string }
variable "project" {
  type    = string
  default = "ds21-llm-serving"
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
variable "serving_instance_types" {
  description = "In order of preference. Four-GPU nodes: tensor and expert parallelism, prefill/decode disaggregation and replica counts are all measured on one node, so the GPUs of a node are the unit. The T4 node (g4dn.12xlarge) is the only four-GPU type that EC2 would launch during the build: g6e.12xlarge, g6.12xlarge and g5.12xlarge were refused for capacity in every zone of us-east-1, us-east-2 and us-west-2. It has no NVLink: every GPU-to-GPU transfer goes over PCIe, and the results say so."
  type        = list(string)
  default     = ["g4dn.12xlarge"]
}
variable "serving_nodes" {
  description = "How many serving nodes to create the pool with. It starts empty: a pool whose first node cannot be launched fails to create, so the tests scale it up afterwards and try again as often as needed."
  type        = number
  default     = 0
}
variable "loadgen_nodes" {
  description = "CPU nodes that run the load generator, so that the generator never competes with the servers for CPU."
  type        = number
  default     = 1
}
