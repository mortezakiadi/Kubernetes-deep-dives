terraform {
  required_version = ">= 1.9"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
# The first region is the main one and the home of IPAM. The second is where the second cluster and the data-center stand-in live.
provider "aws" {
  region = var.regions.main
  # Hard guard: Terraform refuses to run against any other account.
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Purpose   = "design-05-networking"
      Teardown  = "delete-after-use"
    }
  }
}
provider "aws" {
  alias               = "second"
  region              = var.regions.second
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Purpose   = "design-05-networking"
      Teardown  = "delete-after-use"
    }
  }
}
