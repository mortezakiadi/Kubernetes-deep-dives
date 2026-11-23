# Standard EKS (not Auto Mode): the nodes are EKS managed node groups whose AMI release, and with it the NVIDIA
# driver and CUDA user space, is pinned by this file and changes only when this file changes. See the appendix,
# section 2, for why that matters for post-training.
#
# Control-plane logging is off: nothing in this build reads it, and it would create a log group named by EKS.
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

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_route.private_nat,
  ]
}

# ---- add-ons that nodes need before they can become Ready --------------------------------------------------------
locals {
  addon_versions = {
    "vpc-cni"                = "v1.22.4-eksbuild.14"
    "kube-proxy"             = "v1.37.0-eksbuild.39"
    "eks-pod-identity-agent" = "v1.4.1-eksbuild.3"
    "coredns"                = "v1.14.7-eksbuild.35"
    "metrics-server"         = "v0.9.1-eksbuild.2"
  }
}

resource "aws_eks_addon" "before_nodes" {
  for_each      = toset(["vpc-cni", "kube-proxy", "eks-pod-identity-agent"])
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = each.key
  addon_version = local.addon_versions[each.key]
  # The reward sandboxes rely on NetworkPolicy for "no network"; the VPC CNI enforces it only when this is on.
  configuration_values        = each.key == "vpc-cni" ? jsonencode({ enableNetworkPolicy = "true" }) : null
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

# ---- nodes --------------------------------------------------------------------------------------------------------
# System pool: controllers (Kueue, JobSet), the reward verifiers, the health monitor and CPU jobs.
resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.gpu_ami_release
  instance_types  = ["m6i.2xlarge"]
  capacity_type   = "ON_DEMAND"
  disk_size       = 50
  labels          = { "ds08/pool" = "system" }
  scaling_config {
    desired_size = 2
    min_size     = 2
    max_size     = 3
  }
  update_config { max_unavailable = 1 }
  depends_on = [aws_eks_addon.before_nodes, aws_iam_role_policy_attachment.node]
}

# GPU pool: one GPU per node. Kueue is the only way onto it: the taint is tolerated only by pods that Kueue
# admitted (the toleration comes from the queue's ResourceFlavor).
resource "aws_eks_node_group" "gpu" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-gpu"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_NVIDIA"
  release_version = var.gpu_ami_release
  instance_types  = var.gpu_instance_types
  capacity_type   = "ON_DEMAND"
  disk_size       = 200 # the vLLM image is about 10 GB compressed, and checkpoints are staged on local disk
  labels          = { "ds08/pool" = "gpu" }
  taint {
    key    = "nvidia.com/gpu"
    value  = "present"
    effect = "NO_SCHEDULE"
  }
  scaling_config {
    desired_size = var.gpu_nodes
    min_size     = 0
    max_size     = max(var.gpu_nodes, 1)
  }
  update_config { max_unavailable = 1 }
  timeouts {
    create = "40m"
  }
  depends_on = [aws_eks_addon.before_nodes, aws_iam_role_policy_attachment.node]
}

resource "aws_eks_addon" "after_nodes" {
  for_each                    = toset(["coredns", "metrics-server"])
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = local.addon_versions[each.key]
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  configuration_values        = jsonencode({ nodeSelector = { "ds08/pool" = "system" } })
  depends_on                  = [aws_eks_node_group.system]
}

# ---- storage for environment snapshots (Design 08, "Branching rollouts") -----------------------------------------
# The EBS CSI driver provisions environment volumes and takes CSI VolumeSnapshots of them; the snapshot controller
# serves the snapshot.storage.k8s.io API. The driver's controller gets its AWS identity through EKS Pod Identity.
data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.project}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = "v1.68.1-eksbuild.1"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  configuration_values        = jsonencode({ controller = { nodeSelector = { "ds08/pool" = "system" } } })
  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }
  depends_on = [aws_eks_node_group.system, aws_iam_role_policy_attachment.ebs_csi]
}

resource "aws_eks_addon" "snapshot_controller" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "snapshot-controller"
  addon_version               = "v8.7.0-eksbuild.5"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  depends_on                  = [aws_eks_node_group.system]
}
