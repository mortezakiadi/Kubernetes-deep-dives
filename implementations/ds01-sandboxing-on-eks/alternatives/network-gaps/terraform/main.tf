# Two changes to the network layer, both narrower than what is deployed.
#
# 1. Rules for the sandbox nodes that name ports. The deployed rules (terraform/10-cluster/network.tf) allow every protocol and port
#    between the sandbox group and the cluster group, and all egress. To use these, delete those three rules in the same apply:
#    security group rules add up, so leaving the old ones in place makes these ones decorative.
# 2. A security group for the egress gateway pods, used through securitygrouppolicy.yaml.
#
# The ports are what the design is known to use. A node needs more than that to join a cluster and pull images, so a missed port
# shows up as nodes that never become Ready. Try this on a throwaway cluster first.

# --- 1. the sandbox nodes ---------------------------------------------------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "sandbox_kubelet" {
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 10250
  to_port                      = 10250
  description                  = "Control plane to kubelet"
}

resource "aws_vpc_security_group_ingress_rule" "sandbox_sandboxd" {
  for_each                     = toset(["8080", "9090"])
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  description                  = "Sandbox router to sandboxd (files and commands)"
}

resource "aws_vpc_security_group_egress_rule" "sandbox_to_cluster" {
  for_each                     = toset(["443", "3128"])
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  description                  = each.value == "443" ? "Kubelet to the API server" : "Sandboxes to the egress gateway"
}

resource "aws_vpc_security_group_egress_rule" "sandbox_images" {
  security_group_id = var.sandbox_sg_id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Image pulls through the NAT gateway. Narrow to the registry endpoints once the images come from a mirror."
}

resource "aws_vpc_security_group_egress_rule" "sandbox_dns" {
  for_each          = toset(["udp", "tcp"])
  security_group_id = var.sandbox_sg_id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
  description       = "DNS inside the VPC"
}

# --- 2. the egress gateway pods ---------------------------------------------------------------------------------------------------
resource "aws_security_group" "gateway_pods" {
  name        = "${var.project}-egress-gateway-pods"
  description = "Egress gateway pods: port 3128 from sandbox nodes only"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.project}-egress-gateway-pods" }
}

resource "aws_vpc_security_group_ingress_rule" "gateway_from_sandboxes" {
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = var.sandbox_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 3128
  to_port                      = 3128
  description                  = "Proxy port, from sandbox nodes only"
}

# The gateway replicas must be able to reach each other and be probed by the kubelet. A security group made for a policy has no
# ingress rules at all, and the documentation says same-policy pods then cannot talk, so this is the self-reference it asks for.
resource "aws_vpc_security_group_ingress_rule" "gateway_self" {
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = aws_security_group.gateway_pods.id
  ip_protocol                  = "-1"
  description                  = "Self-referencing rule required for pods under one SecurityGroupPolicy"
}

resource "aws_vpc_security_group_ingress_rule" "gateway_probes" {
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 3128
  to_port                      = 3128
  description                  = "Readiness probe from the node"
}

resource "aws_vpc_security_group_egress_rule" "gateway_internet" {
  security_group_id = aws_security_group.gateway_pods.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "The internet, on 443 only. Which domains is the job of the gateway and its ceiling policy."
}

resource "aws_vpc_security_group_egress_rule" "gateway_to_platform" {
  for_each                     = toset(["8000", "8001", "8003", "8443"])
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  description                  = "Manager 8000, credential broker 8001, tool gateway 8003, metrics API 8443"
}

resource "aws_vpc_security_group_egress_rule" "gateway_dns" {
  for_each          = toset(["udp", "tcp"])
  security_group_id = aws_security_group.gateway_pods.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
  description       = "DNS inside the VPC"
}

output "gateway_pod_sg_id" { value = aws_security_group.gateway_pods.id }
