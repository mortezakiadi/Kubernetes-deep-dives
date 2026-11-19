# Design 01, "The sandbox node pool and pod hardening":
# "Separate subnets and security groups: sandbox nodes live in their own subnets,
#  so VPC-level rules can treat them differently from the rest of the cluster."

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-igw" }
}

# --- public subnets: only the NAT gateway lives here -------------------------
resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.this.id
  availability_zone       = var.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = false
  tags = {
    Name                     = "${var.project}-public-${var.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-nat" }
}

# One NAT gateway keeps the cost down. A production build uses one per AZ.
resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.project}-nat" }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- private subnets: "system" for the control plane add-ons and gateways -----
resource "aws_subnet" "system" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, 1 + count.index) # 10.20.16.0/20, 10.20.32.0/20
  tags = {
    Name                              = "${var.project}-system-${var.azs[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
    "ds01/tier"                       = "system"
  }
}

# --- private subnets: "sandbox" for untrusted code only -----------------------
resource "aws_subnet" "sandbox" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, 4 + count.index) # 10.20.64.0/20, 10.20.80.0/20
  tags = {
    Name        = "${var.project}-sandbox-${var.azs[count.index]}"
    "ds01/tier" = "sandbox"
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-private" }
}

resource "aws_route" "private_nat" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this.id
}

resource "aws_route_table_association" "system" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.system[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "sandbox" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.sandbox[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- security group for sandbox nodes ----------------------------------------
# Nodes accept traffic from the cluster security group (control plane, gateways)
# and nothing else. The cluster security group in turn accepts the sandbox nodes.
resource "aws_security_group" "sandbox_nodes" {
  name        = "${var.project}-sandbox-nodes"
  description = "Nodes that run untrusted sandboxes"
  vpc_id      = aws_vpc.this.id
  tags = {
    Name        = "${var.project}-sandbox-nodes"
    "ds01/tier" = "sandbox"
  }
}

resource "aws_vpc_security_group_ingress_rule" "sandbox_from_cluster" {
  security_group_id            = aws_security_group.sandbox_nodes.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "-1"
  description                  = "Control plane and platform pods to sandbox nodes"
}

resource "aws_vpc_security_group_ingress_rule" "cluster_from_sandbox" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.sandbox_nodes.id
  ip_protocol                  = "-1"
  description                  = "Sandbox nodes to control plane and the egress gateway"
}

resource "aws_vpc_security_group_egress_rule" "sandbox_all" {
  security_group_id = aws_security_group.sandbox_nodes.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Node-level egress. Pod egress is controlled by network policy."
}
