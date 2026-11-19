# Four EKS Auto Mode clusters created from ONE definition (Design 02: "every cluster is created from the
# same definition"). The role label says what the cluster is for; the platform layer reads it.

resource "aws_cloudwatch_log_group" "cluster" {
  for_each          = var.clusters
  name              = "/aws/eks/${var.project}-${each.key}/cluster"
  retention_in_days = 3
}

resource "aws_eks_cluster" "this" {
  for_each = var.clusters

  name     = "${var.project}-${each.key}"
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    subnet_ids = aws_subnet.private[*].id
    # The administrator reaches the API from the public endpoint (one /32). The hub reaches the workers over
    # the private endpoint from inside the VPC, so no worker API is open to the internet beyond that /32.
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.admin_cidr]
  }

  compute_config {
    enabled       = true
    node_pools    = ["general-purpose", "system"]
    node_role_arn = aws_iam_role.node.arn
  }

  kubernetes_network_config {
    elastic_load_balancing { enabled = true }
  }

  storage_config {
    block_storage { enabled = true }
  }

  enabled_cluster_log_types = ["api", "audit"]

  tags = { "ds02/role" = each.value.role }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
    aws_route.private_nat,
  ]
}

# The hub (Kueue manager) calls the workers' API servers from inside the VPC.
resource "aws_vpc_security_group_ingress_rule" "api_from_vpc" {
  for_each          = { for k, v in var.clusters : k => v if v.role != "manager" }
  security_group_id = aws_eks_cluster.this[each.key].vpc_config[0].cluster_security_group_id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Hub cluster (MultiKueue) to the API server of this worker"
}
