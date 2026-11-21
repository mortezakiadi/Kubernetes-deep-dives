variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}
variable "project" {
  description = "Name prefix and the Project tag on every resource, used by the teardown check."
  type        = string
  default     = "ds05-networking"
}
variable "regions" {
  type = object({ main = string, second = string })
  default = {
    main   = "us-east-1"
    second = "eu-west-1"
  }
}
