# EKS Auto Mode cluster. Design 01 names Nitro Sandboxes (runtimeClassName
# isolated-sandbox) as the default isolation tier, and Auto Mode is the setup
# path AWS documents for them. Nitro Sandboxes need Kubernetes 1.36 or later.

locals {
  cluster_name = var.project
}

# ---- cluster role -------------------------------------------------------------
data "aws_iam_policy_document" "cluster_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.project}-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_trust.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each = toset([
    "AmazonEKSClusterPolicy",
    "AmazonEKSComputePolicy",
    "AmazonEKSBlockStoragePolicy",
    "AmazonEKSLoadBalancingPolicy",
    "AmazonEKSNetworkingPolicy",
  ])
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

# ---- node role (built-in pools) ----------------------------------------------
data "aws_iam_policy_document" "node_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.project}-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodeMinimalPolicy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

# ---- node role for the sandbox pool ------------------------------------------
# Design 01: "Minimal node role: only what the kubelet and CNI need. No S3, no
# Secrets Manager, nothing an escaped process could use." It is a separate role
# from the platform nodes, and carries no image-pull permission beyond ECR read.
resource "aws_iam_role" "sandbox_node" {
  name               = "${var.project}-sandbox-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "sandbox_node" {
  for_each = toset([
    "AmazonEKSWorkerNodeMinimalPolicy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.sandbox_node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

# ---- control plane logs ------------------------------------------------------
# Created first so Terraform owns (and deletes) the log group.
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${local.cluster_name}/cluster"
  retention_in_days = 7
}

# ---- the cluster -------------------------------------------------------------
resource "aws_eks_cluster" "this" {
  name     = local.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  # Auto Mode manages the core add-ons itself.
  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  vpc_config {
    # Platform pods (controller, gateways) go in the system subnets. The sandbox
    # subnets are reached through a dedicated NodeClass, not through this list.
    subnet_ids              = aws_subnet.system[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.admin_cidr]
  }

  # Auto Mode requires compute, load balancing and block storage together.
  compute_config {
    enabled       = true
    node_pools    = ["general-purpose", "system"]
    node_role_arn = aws_iam_role.node.arn
  }

  kubernetes_network_config {
    elastic_load_balancing {
      enabled = true
    }
  }

  storage_config {
    block_storage {
      enabled = true
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
    aws_route.private_nat,
  ]
}

# The sandbox NodeClass uses its own node role, which needs its own access entry
# (EKS creates one automatically only for the built-in node class).
resource "aws_eks_access_entry" "sandbox_node" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.sandbox_node.arn
  type          = "EC2"
}

resource "aws_eks_access_policy_association" "sandbox_node" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.sandbox_node.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAutoNodePolicy"
  access_scope {
    type = "cluster"
  }
  depends_on = [aws_eks_access_entry.sandbox_node]
}
