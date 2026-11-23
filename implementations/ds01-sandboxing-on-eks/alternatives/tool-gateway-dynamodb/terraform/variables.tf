variable "project" {
  type    = string
  default = "ds01-sandboxing"
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "cluster_name" {
  type = string
}
variable "vpc_id" {
  type = string
}
variable "private_route_table_ids" {
  type = list(string)
}
