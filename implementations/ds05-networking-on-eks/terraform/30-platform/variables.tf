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
