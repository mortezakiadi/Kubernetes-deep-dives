terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
provider "aws" {
  region = var.region
  # Hard guard: Terraform refuses to run against any other account.
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Purpose   = "design-03-training-inference"
      Teardown  = "delete-after-use"
    }
  }
}
