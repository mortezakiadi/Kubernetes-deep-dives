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
    public_access_cidrs     = [for ip in split(",", var.admin_ips) : "${ip}/32"]
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
# System pool: controllers, the rule-based verifiers, the store, the registry, the monitor and CPU jobs.
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
  labels          = { "ds10/pool" = "system" }
  scaling_config {
    desired_size = 2
    min_size     = 2
    max_size     = 3
  }
  update_config { max_unavailable = 1 }
  depends_on = [aws_eks_addon.before_nodes, aws_iam_role_policy_attachment.node]
}

# GPU pool: one GPU per node. Two serve the judges (one model each); one runs the red-team RL runs.
resource "aws_eks_node_group" "gpu" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-gpu"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_NVIDIA"
  release_version = var.gpu_ami_release
  instance_types  = var.gpu_instance_types
  capacity_type   = "ON_DEMAND"
  disk_size       = 200 # the vLLM image is about 10 GB compressed, and model weights are cached on local disk
  labels          = { "ds10/pool" = "gpu" }
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
  configuration_values        = jsonencode({ nodeSelector = { "ds10/pool" = "system" } })
  depends_on                  = [aws_eks_node_group.system]
}


# ---- sandbox pool: code verification under gVisor (Design 10, "Code verification"; Design 01, "Sandboxing") --------
# One node whose containerd knows a second runtime, runsc. Pods that name the gvisor RuntimeClass run on it inside
# gVisor's user-space kernel; nothing else may schedule here (taint). The release is pinned by date and checked
# against a SHA-512 written in this file, not one downloaded next to the tarball.
locals {
  gvisor_release    = "20261116.0"
  gvisor_sha512     = "c87d792d5842efb70c38ee822124cfe0821a547715e7abcf3f2b14997cd02784edaa0b91e238ec5c27a25a1b9e00bf98113398eb62a0778d8d20d4e222f8b0e0"
  sandbox_user_data = <<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="//"

    --//
    Content-Type: application/node.eks.aws

    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      containerd:
        config: |
          [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runsc]
            runtime_type = "io.containerd.runsc.v1"
          [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
            runtime_type = "io.containerd.runsc.v1"

    --//
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    set -euxo pipefail
    command -v bzip2 || dnf install -y bzip2
    cd /tmp
    curl -fsSLo gvisor.tar.bz2 "https://storage.googleapis.com/gvisor/releases/release/${local.gvisor_release}/x86_64/gvisor.tar.bz2"
    echo "${local.gvisor_sha512}  gvisor.tar.bz2" | sha512sum -c -
    tar -xjf gvisor.tar.bz2 -C /usr/local/bin
    chmod -R a+rx /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 /usr/local/bin/gvisor-bin
    /usr/local/bin/runsc --version

    --//--
  EOT
}

resource "aws_launch_template" "sandbox" {
  name_prefix = "${var.project}-sandbox-"
  user_data   = base64encode(local.sandbox_user_data)
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1 # pods cannot reach the instance metadata service
  }
  tag_specifications {
    resource_type = "instance"
    tags          = { Project = var.project, Name = "${var.project}-sandbox" }
  }
}

resource "aws_eks_node_group" "sandbox" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-sandbox"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_STANDARD"
  release_version = var.gpu_ami_release
  instance_types  = ["m6i.xlarge"]
  capacity_type   = "ON_DEMAND"
  labels          = { "ds10/pool" = "sandbox", "ds10/runtime" = "gvisor" }
  taint {
    key    = "ds10/sandbox"
    value  = "gvisor"
    effect = "NO_SCHEDULE"
  }
  launch_template {
    id      = aws_launch_template.sandbox.id
    version = aws_launch_template.sandbox.latest_version
  }
  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 2
  }
  update_config { max_unavailable = 1 }
  depends_on = [aws_eks_addon.before_nodes, aws_iam_role_policy_attachment.node]
}
