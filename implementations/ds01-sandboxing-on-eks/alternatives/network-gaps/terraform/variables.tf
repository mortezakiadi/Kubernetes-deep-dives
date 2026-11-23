variable "vpc_id" { type = string }
variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}
variable "sandbox_sg_id" {
  description = "The security group of the sandbox nodes (aws_security_group.sandbox_nodes in terraform/10-cluster)."
  type        = string
}
variable "cluster_sg_id" {
  description = "The EKS cluster security group, which the control plane and the platform pods use."
  type        = string
}
variable "project" {
  type    = string
  default = "ds01"
}
