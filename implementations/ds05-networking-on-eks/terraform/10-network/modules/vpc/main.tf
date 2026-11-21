# One VPC of the Design 05 network. The same module builds every kind of VPC in the design:
#   - a cluster VPC (node subnets, an optional second address range for pods, NAT),
#   - a small VPC for a test host (a stand-in for a shared service, a legacy network or a data center).
# Its address comes either from an IPAM pool (the planned way) or from a literal range (only for the legacy VPC that the plan never saw).
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "name" { type = string }
variable "project" { type = string }
variable "ipam_pool_id" {
  type    = string
  default = null
}
variable "netmask_length" {
  type    = number
  default = null
}
variable "cidr_block" {
  type    = string
  default = null
}
variable "az_count" {
  type    = number
  default = 2
}
variable "subnet_newbits" {
  description = "Bits added to the VPC range for each subnet (4 gives /20 from a /16, 2 gives /26 from a /24)."
  type        = number
}
variable "pod_cidr" {
  description = "Optional second range for pods (100.64.0.0/16), never routed outside the VPC."
  type        = string
  default     = null
}
variable "tiny_pod_subnet" {
  description = "Add one deliberately small pod subnet (a /26) for the address-exhaustion test."
  type        = bool
  default     = false
}
variable "nat" {
  type    = bool
  default = false
}
variable "host" {
  description = "Put a small test host in the first subnet, reachable only through Systems Manager."
  type        = bool
  default     = false
}
variable "host_script" {
  description = "Extra shell run on the host at first boot."
  type        = string
  default     = ""
}
variable "core_network_arn" {
  description = "Set it, together with attachment_id, to route the plan's whole range to the core network."
  type        = string
  default     = null
}
variable "route_to_core" {
  type    = bool
  default = false
}
variable "attachment_id" {
  type    = string
  default = null
}
variable "routed_range" {
  type    = string
  default = "10.60.0.0/14"
}
variable "flow_log_bucket_arn" {
  type    = string
  default = null
}

data "aws_availability_zones" "this" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
data "aws_region" "this" {}

locals {
  azs = slice(data.aws_availability_zones.this.names, 0, var.az_count)
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  ipv4_ipam_pool_id    = var.ipam_pool_id
  ipv4_netmask_length  = var.netmask_length
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-${var.name}" }
}

resource "aws_vpc_ipv4_cidr_block_association" "pods" {
  count      = var.pod_cidr == null ? 0 : 1
  vpc_id     = aws_vpc.this.id
  cidr_block = var.pod_cidr
}

# Node (or host) subnets, one per zone.
resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, var.subnet_newbits, count.index)
  tags = {
    Name                              = "${var.project}-${var.name}-private-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

# Pod subnets (only for the cluster VPCs): one /18 per zone from the second range.
resource "aws_subnet" "pods" {
  count             = var.pod_cidr == null ? 0 : var.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.pod_cidr, 2, count.index)
  tags = {
    Name                         = "${var.project}-${var.name}-pods-${local.azs[count.index]}"
    "${var.project}/subnet-role" = "pods"
  }
  depends_on = [aws_vpc_ipv4_cidr_block_association.pods]
}

resource "aws_subnet" "tiny" {
  count             = var.tiny_pod_subnet ? 1 : 0
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[0]
  cidr_block        = cidrsubnet(var.pod_cidr, 10, 1020)
  tags              = { Name = "${var.project}-${var.name}-pods-tiny" }
  depends_on        = [aws_vpc_ipv4_cidr_block_association.pods]
}
resource "aws_route_table_association" "tiny" {
  count          = var.tiny_pod_subnet ? 1 : 0
  subnet_id      = aws_subnet.tiny[0].id
  route_table_id = aws_route_table.private.id
}

# Public subnets and a NAT gateway, only where something needs to reach the internet.
resource "aws_internet_gateway" "this" {
  count  = var.nat ? 1 : 0
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-${var.name}" }
}
resource "aws_subnet" "public" {
  count             = var.nat ? var.az_count : 0
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, var.subnet_newbits, 8 + count.index)
  tags = {
    Name                     = "${var.project}-${var.name}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}
resource "aws_eip" "nat" {
  count  = var.nat ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${var.project}-${var.name}-nat" }
}
resource "aws_nat_gateway" "this" {
  count         = var.nat ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.project}-${var.name}" }
  depends_on    = [aws_internet_gateway.this]
}
resource "aws_route_table" "public" {
  count  = var.nat ? 1 : 0
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-${var.name}-public" }
}
resource "aws_route" "public_internet" {
  count                  = var.nat ? 1 : 0
  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}
resource "aws_route_table_association" "public" {
  count          = var.nat ? var.az_count : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# One route table for every private and pod subnet. Cloud WAN adds its own routes to it (see the attachment in the root module).
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-${var.name}-private" }
}
resource "aws_route" "private_nat" {
  count                  = var.nat ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}
# The one route that sends everything in the plan to the core network. The VPC's own range stays local (it is more specific).
resource "terraform_data" "attachment_ready" {
  input = var.attachment_id
}
resource "aws_route" "core" {
  count                  = var.route_to_core ? 1 : 0
  depends_on             = [terraform_data.attachment_ready]
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = var.routed_range
  core_network_arn       = var.core_network_arn
}
resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
resource "aws_route_table_association" "pods" {
  count          = var.pod_cidr == null ? 0 : var.az_count
  subnet_id      = aws_subnet.pods[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free gateway endpoint for S3 in every VPC (Design 05, "Cost and traffic engineering").
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.this.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.project}-${var.name}-s3" }
}

# VPC flow logs to S3, for the cost and traffic scenarios.
resource "aws_flow_log" "this" {
  count                    = var.flow_log_bucket_arn == null ? 0 : 1
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${var.flow_log_bucket_arn}/${var.name}/"
  max_aggregation_interval = 60
  log_format               = "$${version} $${srcaddr} $${dstaddr} $${srcport} $${dstport} $${protocol} $${bytes} $${packets} $${start} $${end} $${action} $${az-id} $${pkt-srcaddr} $${pkt-dstaddr} $${flow-direction} $${subnet-id} $${interface-id}"
  tags                     = { Name = "${var.project}-${var.name}" }
}

# ----------------------------------------------------------------------------------------------- the test host --
data "aws_ssm_parameter" "ami" {
  count = var.host ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "host" {
  count       = var.host ? 1 : 0
  name        = "${var.project}-${var.name}-host"
  description = "Test host: open to every private range of the plan, so that the network layers, not this group, decide what can reach it"
  vpc_id      = aws_vpc.this.id
  ingress {
    description = "Everything from the ranges of the plan"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.60.0.0/14", "100.64.0.0/16", "172.16.0.0/12"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-${var.name}-host" }
}

resource "aws_security_group" "endpoints" {
  count       = var.host ? 1 : 0
  name        = "${var.project}-${var.name}-endpoints"
  description = "HTTPS to the Systems Manager endpoints"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.this.cidr_block]
  }
  tags = { Name = "${var.project}-${var.name}-endpoints" }
}

# Systems Manager needs three interface endpoints when the VPC has no internet path.
resource "aws_vpc_endpoint" "ssm" {
  for_each            = var.host ? toset(["ssm", "ssmmessages", "ec2messages"]) : toset([])
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.this.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  tags                = { Name = "${var.project}-${var.name}-${each.key}" }
}

resource "aws_iam_role" "host" {
  count = var.host ? 1 : 0
  name  = "${var.project}-${var.name}-host"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.host ? 1 : 0
  role       = aws_iam_role.host[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
# The caller's own permission to use VPC Lattice. The service's auth policy decides separately who may call it; both must allow (S06).
resource "aws_iam_role_policy" "lattice_invoke" {
  count = var.host ? 1 : 0
  name  = "lattice-invoke"
  role  = aws_iam_role.host[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "vpc-lattice-svcs:Invoke", Resource = "*" }]
  })
}
resource "aws_iam_instance_profile" "host" {
  count = var.host ? 1 : 0
  name  = "${var.project}-${var.name}-host"
  role  = aws_iam_role.host[0].name
}

resource "aws_instance" "host" {
  count                  = var.host ? 1 : 0
  ami                    = data.aws_ssm_parameter.ami[0].value
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.host[0].id]
  iam_instance_profile   = aws_iam_instance_profile.host[0].name
  metadata_options {
    http_tokens = "required"
  }
  user_data  = <<-EOT
    #!/bin/bash
    # A tiny web service that says which VPC it is in, on port 8080.
    mkdir -p /srv/www && echo "${var.name}" > /srv/www/whoami
    cat > /etc/systemd/system/whoami.service <<'UNIT'
    [Unit]
    Description=whoami
    After=network.target
    [Service]
    WorkingDirectory=/srv/www
    ExecStart=/usr/bin/python3 -m http.server 8080
    Restart=always
    [Install]
    WantedBy=multi-user.target
    UNIT
    systemctl daemon-reload && systemctl enable --now whoami
    ${var.host_script}
  EOT
  tags       = { Name = "${var.project}-${var.name}-host" }
  depends_on = [aws_vpc_endpoint.ssm, aws_vpc_endpoint.s3, aws_route_table_association.private]
}

output "vpc_id" { value = aws_vpc.this.id }
output "vpc_arn" { value = aws_vpc.this.arn }
output "cidr_block" { value = aws_vpc.this.cidr_block }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "private_subnet_arns" { value = aws_subnet.private[*].arn }
output "pod_subnet_ids" { value = aws_subnet.pods[*].id }
output "tiny_pod_subnet_id" { value = var.tiny_pod_subnet ? aws_subnet.tiny[0].id : null }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "route_table_id" { value = aws_route_table.private.id }
output "host_id" { value = var.host ? aws_instance.host[0].id : null }
output "host_ip" { value = var.host ? aws_instance.host[0].private_ip : null }
output "azs" { value = local.azs }
output "host_sg_id" { value = var.host ? aws_security_group.host[0].id : null }
output "host_role_arn" { value = var.host ? aws_iam_role.host[0].arn : null }
