# The address plan (Design 05, "The address plan"): one IPAM, one top-level range, a pool per region, a pool per environment in each region.
# Every VPC that has to be routed takes its range from a pool, so two of them can never overlap.
resource "aws_vpc_ipam" "this" {
  description = "${var.project} address plan"
  tier        = "advanced"
  operating_regions { region_name = var.regions.main }
  operating_regions { region_name = var.regions.second }
}

locals {
  scope = aws_vpc_ipam.this.private_default_scope_id
  # name => [region key, parent name, range provisioned into the pool]
  pools = {
    top            = { locale = null, parent = null, cidr = "10.60.0.0/14" }
    main           = { locale = var.regions.main, parent = "top", cidr = "10.60.0.0/15" }
    second         = { locale = var.regions.second, parent = "top", cidr = "10.62.0.0/15" }
    main-prod      = { locale = var.regions.main, parent = "main", cidr = "10.60.0.0/16" }
    main-nonprod   = { locale = var.regions.main, parent = "main", cidr = "10.61.0.0/24" }
    main-shared    = { locale = var.regions.main, parent = "main", cidr = "10.61.1.0/24" }
    second-prod    = { locale = var.regions.second, parent = "second", cidr = "10.62.0.0/16" }
    second-hybrid  = { locale = var.regions.second, parent = "second", cidr = "10.63.0.0/24" }
    second-sandbox = { locale = var.regions.second, parent = "second", cidr = "10.63.1.0/24" }
  }
}

# Pools are created level by level, because a child needs its parent's range to exist first.
resource "aws_vpc_ipam_pool" "top" {
  address_family = "ipv4"
  ipam_scope_id  = local.scope
  description    = "all routed addresses of the platform"
}
resource "aws_vpc_ipam_pool_cidr" "top" {
  ipam_pool_id = aws_vpc_ipam_pool.top.id
  cidr         = local.pools.top.cidr
}

resource "aws_vpc_ipam_pool" "region" {
  for_each            = { main = local.pools.main, second = local.pools.second }
  address_family      = "ipv4"
  ipam_scope_id       = local.scope
  source_ipam_pool_id = aws_vpc_ipam_pool.top.id
  locale              = each.value.locale
  description         = "region ${each.value.locale}"
  depends_on          = [aws_vpc_ipam_pool_cidr.top]
}
resource "aws_vpc_ipam_pool_cidr" "region" {
  for_each     = aws_vpc_ipam_pool.region
  ipam_pool_id = each.value.id
  cidr         = local.pools[each.key].cidr
}

resource "aws_vpc_ipam_pool" "env" {
  for_each            = { for k, v in local.pools : k => v if v.parent != null && v.parent != "top" }
  address_family      = "ipv4"
  ipam_scope_id       = local.scope
  source_ipam_pool_id = aws_vpc_ipam_pool.region[each.value.parent].id
  locale              = each.value.locale
  description         = each.key
  depends_on          = [aws_vpc_ipam_pool_cidr.region]
}
resource "aws_vpc_ipam_pool_cidr" "env" {
  for_each     = aws_vpc_ipam_pool.env
  ipam_pool_id = each.value.id
  cidr         = local.pools[each.key].cidr
}
