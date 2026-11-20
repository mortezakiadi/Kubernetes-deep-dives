# One EKS Auto Mode cluster. It runs the durable execution engine, the agent workers, the gateways and the
# sandboxes. No GPU is needed: the model is behind a gateway, and Design 03 covers serving it.
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.project}/cluster"
  retention_in_days = 3
}
resource "aws_eks_cluster" "this" {
  name                          = var.project
  version                       = var.kubernetes_version
  role_arn                      = aws_iam_role.cluster.arn
  bootstrap_self_managed_addons = false
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }
  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
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
  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
    aws_route.private_nat,
  ]
}
