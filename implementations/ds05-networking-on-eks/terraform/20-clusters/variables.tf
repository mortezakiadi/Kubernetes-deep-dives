variable "account_id" { type = string }
variable "project" {
  type    = string
  default = "ds05-networking"
}
variable "regions" {
  type = object({ main = string, second = string })
  default = {
    main   = "us-east-1"
    second = "eu-west-1"
  }
}
variable "kubernetes_version" {
  type    = string
  default = "1.37"
}
variable "admin_cidr" {
  description = "Public IP (as a /32) allowed to reach the cluster API endpoints. up.sh and scripts/update-my-ip.sh fill this in."
  type        = string
}
