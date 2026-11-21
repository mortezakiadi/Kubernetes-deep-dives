# The VPCs of the build. Six are routed through the core network; one (legacy-main) is deliberately outside the plan.
#   prod-main      cluster VPC   segment prod      main region
#   prod-second    cluster VPC   segment prod      second region
#   nonprod-main   test host     segment nonprod
#   shared-main    test host     segment shared    (shared services, private DNS zone)
#   hybrid-second  test host     segment hybrid    (stand-in for the on-premises data center: it has a DNS server of its own)
#   sandbox-second test host     segment sandbox
#   legacy-main    test host     NOT attached      (reuses a range that is also in the plan: Lattice has to reach it)
module "prod_main" {
  source           = "./modules/vpc"
  name             = "prod-main"
  project          = var.project
  ipam_pool_id     = aws_vpc_ipam_pool.env["main-prod"].id
  netmask_length   = 16
  az_count         = 3
  subnet_newbits   = 4
  pod_cidr         = "100.64.0.0/16"
  nat              = true
  tiny_pod_subnet  = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["prod-main"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
module "prod_second" {
  source           = "./modules/vpc"
  providers        = { aws = aws.second }
  name             = "prod-second"
  project          = var.project
  ipam_pool_id     = aws_vpc_ipam_pool.env["second-prod"].id
  netmask_length   = 16
  az_count         = 3
  subnet_newbits   = 4
  pod_cidr         = "100.64.0.0/16"
  nat              = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["prod-second"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
module "nonprod_main" {
  source           = "./modules/vpc"
  name             = "nonprod-main"
  project          = var.project
  ipam_pool_id     = aws_vpc_ipam_pool.env["main-nonprod"].id
  netmask_length   = 24
  az_count         = 2
  subnet_newbits   = 2
  host             = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["nonprod-main"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
module "shared_main" {
  source           = "./modules/vpc"
  name             = "shared-main"
  project          = var.project
  ipam_pool_id     = aws_vpc_ipam_pool.env["main-shared"].id
  netmask_length   = 24
  az_count         = 2
  subnet_newbits   = 2
  host             = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["shared-main"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
module "hybrid_second" {
  source    = "./modules/vpc"
  providers = { aws = aws.second }
  name      = "hybrid-second"
  project   = var.project
  # The data center's own DNS server: it answers for corp.dc.test and forwards ds05.internal to the inbound resolver endpoint in AWS.
  host_script      = <<-SCRIPT
    dnf install -y dnsmasq bind-utils
    # The packaged configuration binds to interfaces at start; bind-dynamic waits for an address, so the server survives a reboot.
    sed -i 's/^bind-interfaces/#bind-interfaces/' /etc/dnsmasq.conf
    MYIP=$(hostname -I | awk '{print $1}')
    cat > /etc/dnsmasq.d/dc.conf <<CONF
    listen-address=$MYIP
    listen-address=127.0.0.1
    bind-dynamic
    no-resolv
    log-queries
    log-facility=/var/log/dnsmasq.log
    host-record=registry.corp.dc.test,$MYIP
    ${join("\n", [for a in aws_route53_resolver_endpoint.inbound.ip_address : "server=/ds05.internal/${a.ip}"])}
    CONF
    systemctl enable --now dnsmasq
  SCRIPT
  ipam_pool_id     = aws_vpc_ipam_pool.env["second-hybrid"].id
  netmask_length   = 24
  az_count         = 2
  subnet_newbits   = 2
  host             = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["hybrid-second"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
module "sandbox_second" {
  source           = "./modules/vpc"
  providers        = { aws = aws.second }
  name             = "sandbox-second"
  project          = var.project
  ipam_pool_id     = aws_vpc_ipam_pool.env["second-sandbox"].id
  netmask_length   = 24
  az_count         = 2
  subnet_newbits   = 2
  host             = true
  route_to_core    = true
  core_network_arn = aws_networkmanager_core_network.this.arn
  attachment_id    = aws_networkmanager_vpc_attachment.this["sandbox-second"].id
  depends_on       = [aws_vpc_ipam_pool_cidr.env]
}
# Not in the plan: a range copied from prod-main by hand. It is not attached to the core network, so it overlaps with nothing that routes.
module "legacy_main" {
  source         = "./modules/vpc"
  name           = "legacy-main"
  project        = var.project
  cidr_block     = "10.60.0.0/16"
  az_count       = 2
  subnet_newbits = 8
  host           = true
}
