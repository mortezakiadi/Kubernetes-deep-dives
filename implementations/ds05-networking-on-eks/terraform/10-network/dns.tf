# DNS across clusters and clouds (Design 05, "DNS across clouds and clusters").
#   ds05.internal   a private hosted zone, shared with the VPCs that are part of the platform (not the data center and not the legacy VPC)
#   corp.dc.test    a zone that only the data center's own DNS server knows
# Resolver endpoints connect the two worlds: an inbound endpoint lets the data center ask AWS, an outbound endpoint plus a forwarding rule lets AWS ask the data center.
locals {
  internal_zone = "ds05.internal"
  dc_zone       = "corp.dc.test"
}

resource "aws_route53_zone" "internal" {
  name = local.internal_zone
  vpc {
    vpc_id     = module.shared_main.vpc_id
    vpc_region = var.regions.main
  }
  lifecycle { ignore_changes = [vpc] }
  tags = { Name = "${var.project}-internal" }
}
resource "aws_route53_zone_association" "main" {
  for_each = { prod-main = module.prod_main.vpc_id, nonprod-main = module.nonprod_main.vpc_id }
  zone_id  = aws_route53_zone.internal.zone_id
  vpc_id   = each.value
}
resource "aws_route53_zone_association" "second" {
  zone_id    = aws_route53_zone.internal.zone_id
  vpc_id     = module.prod_second.vpc_id
  vpc_region = var.regions.second
}
# A shared service (a stand-in for the model registry) with a stable name.
resource "aws_route53_record" "registry" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = "registry.${local.internal_zone}"
  type    = "A"
  ttl     = 30
  records = [module.shared_main.host_ip]
}

# ---- inbound: the data center asks AWS --------------------------------------------------------------------------
resource "aws_security_group" "resolver_in" {
  name        = "${var.project}-resolver-inbound"
  description = "DNS from the ranges of the plan"
  vpc_id      = module.shared_main.vpc_id
  ingress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  ingress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  tags = { Name = "${var.project}-resolver-inbound" }
}
resource "aws_route53_resolver_endpoint" "inbound" {
  name               = "${var.project}-inbound"
  direction          = "INBOUND"
  security_group_ids = [aws_security_group.resolver_in.id]
  dynamic "ip_address" {
    for_each = module.shared_main.private_subnet_ids
    content { subnet_id = ip_address.value }
  }
  tags = { Name = "${var.project}-inbound" }
}

# ---- outbound: AWS asks the data center, one endpoint and rule per region ------------------------------------------
resource "aws_security_group" "resolver_out_main" {
  name        = "${var.project}-resolver-outbound"
  description = "DNS to the data center"
  vpc_id      = module.shared_main.vpc_id
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  tags = { Name = "${var.project}-resolver-outbound" }
}
resource "aws_route53_resolver_endpoint" "outbound_main" {
  name               = "${var.project}-outbound"
  direction          = "OUTBOUND"
  security_group_ids = [aws_security_group.resolver_out_main.id]
  dynamic "ip_address" {
    for_each = module.shared_main.private_subnet_ids
    content { subnet_id = ip_address.value }
  }
  tags = { Name = "${var.project}-outbound" }
}
resource "aws_route53_resolver_rule" "main" {
  name                 = "${var.project}-corp-dc"
  domain_name          = local.dc_zone
  rule_type            = "FORWARD"
  resolver_endpoint_id = aws_route53_resolver_endpoint.outbound_main.id
  target_ip {
    ip   = module.hybrid_second.host_ip
    port = 53
  }
}
resource "aws_route53_resolver_rule_association" "main" {
  for_each         = { prod-main = module.prod_main.vpc_id, shared-main = module.shared_main.vpc_id }
  resolver_rule_id = aws_route53_resolver_rule.main.id
  vpc_id           = each.value
}

resource "aws_security_group" "resolver_out_second" {
  provider    = aws.second
  name        = "${var.project}-resolver-outbound"
  description = "DNS to the data center"
  vpc_id      = module.prod_second.vpc_id
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["10.60.0.0/14"]
  }
  tags = { Name = "${var.project}-resolver-outbound" }
}
resource "aws_route53_resolver_endpoint" "outbound_second" {
  provider           = aws.second
  name               = "${var.project}-outbound"
  direction          = "OUTBOUND"
  security_group_ids = [aws_security_group.resolver_out_second.id]
  dynamic "ip_address" {
    for_each = slice(module.prod_second.private_subnet_ids, 0, 2)
    content { subnet_id = ip_address.value }
  }
  tags = { Name = "${var.project}-outbound" }
}
resource "aws_route53_resolver_rule" "second" {
  provider             = aws.second
  name                 = "${var.project}-corp-dc"
  domain_name          = local.dc_zone
  rule_type            = "FORWARD"
  resolver_endpoint_id = aws_route53_resolver_endpoint.outbound_second.id
  target_ip {
    ip   = module.hybrid_second.host_ip
    port = 53
  }
}
resource "aws_route53_resolver_rule_association" "second" {
  provider         = aws.second
  resolver_rule_id = aws_route53_resolver_rule.second.id
  vpc_id           = module.prod_second.vpc_id
}
