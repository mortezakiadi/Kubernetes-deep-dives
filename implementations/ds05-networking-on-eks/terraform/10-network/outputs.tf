output "project" { value = var.project }
output "core_network_id" { value = aws_networkmanager_core_network.this.id }
output "vpcs" {
  value = {
    prod-main      = { id = module.prod_main.vpc_id, cidr = module.prod_main.cidr_block }
    prod-second    = { id = module.prod_second.vpc_id, cidr = module.prod_second.cidr_block }
    nonprod-main   = { id = module.nonprod_main.vpc_id, cidr = module.nonprod_main.cidr_block, host = module.nonprod_main.host_id, ip = module.nonprod_main.host_ip, sg = module.nonprod_main.host_sg_id, role = module.nonprod_main.host_role_arn }
    shared-main    = { id = module.shared_main.vpc_id, cidr = module.shared_main.cidr_block, host = module.shared_main.host_id, ip = module.shared_main.host_ip, sg = module.shared_main.host_sg_id, role = module.shared_main.host_role_arn }
    hybrid-second  = { id = module.hybrid_second.vpc_id, cidr = module.hybrid_second.cidr_block, host = module.hybrid_second.host_id, ip = module.hybrid_second.host_ip, sg = module.hybrid_second.host_sg_id, role = module.hybrid_second.host_role_arn }
    sandbox-second = { id = module.sandbox_second.vpc_id, cidr = module.sandbox_second.cidr_block, host = module.sandbox_second.host_id, ip = module.sandbox_second.host_ip, sg = module.sandbox_second.host_sg_id, role = module.sandbox_second.host_role_arn }
    legacy-main    = { id = module.legacy_main.vpc_id, cidr = module.legacy_main.cidr_block, host = module.legacy_main.host_id, ip = module.legacy_main.host_ip, sg = module.legacy_main.host_sg_id, role = module.legacy_main.host_role_arn }
  }
}
# What the cluster layer needs from the clusters' VPCs.
output "cluster_vpcs" {
  value = {
    main = {
      vpc_id          = module.prod_main.vpc_id
      cidr            = module.prod_main.cidr_block
      node_subnets    = module.prod_main.private_subnet_ids
      pod_subnets     = module.prod_main.pod_subnet_ids
      tiny_pod_subnet = module.prod_main.tiny_pod_subnet_id
      azs             = module.prod_main.azs
      region          = var.regions.main
    }
    second = {
      vpc_id       = module.prod_second.vpc_id
      cidr         = module.prod_second.cidr_block
      node_subnets = module.prod_second.private_subnet_ids
      pod_subnets  = module.prod_second.pod_subnet_ids
      azs          = module.prod_second.azs
      region       = var.regions.second
    }
  }
}
output "dns" {
  value = {
    internal_zone_id = aws_route53_zone.internal.zone_id
    internal_zone    = local.internal_zone
    dc_zone          = local.dc_zone
    inbound_ips      = [for a in aws_route53_resolver_endpoint.inbound.ip_address : a.ip]
  }
}
