variable "project" {
  type    = string
  default = "ds01-sandboxing"
}
variable "vpc_id" {
  type = string
}
variable "sandbox_subnet_ids" {
  type        = list(string)
  description = "The sandbox subnets (the ones tagged ds01/tier=sandbox)."
}
variable "audit_firehose_arn" {
  type        = string
  description = "Output firehose_arn of ../audit-store."
}
variable "logs_to_firehose_role_arn" {
  type        = string
  description = "Output logs_to_firehose_role_arn of ../audit-store."
}
