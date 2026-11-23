variable "project" {
  type    = string
  default = "ds01-sandboxing"
}
variable "account_id" {
  type = string
}
variable "retention_days" {
  type        = number
  default     = 365
  description = "How long no one can delete or overwrite an audit object. Compliance mode cannot be shortened, so choose with care."
}
variable "platform_log_group_name" {
  type        = string
  description = "The CloudWatch log group that holds the platform pods' output, for example /aws/containerinsights/<cluster>/application."
}
