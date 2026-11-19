variable "account_id" {
  description = "The only AWS account this configuration may touch. Set it in a local, git-ignored tfvars file."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  description = "Must match the fleet layer."
  type        = string
  default     = "ds02-fleet"
}

variable "kueue_version" {
  description = "Pinned across the whole fleet (Design 02: pin Kueue and test upgrades on a staging pair first)."
  type        = string
  default     = "0.20.7"
}
