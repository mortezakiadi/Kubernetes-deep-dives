resource "aws_iam_role" "node" {
  name = "${var.project}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}
# A big root volume: the vLLM image is about 10 GB, and every model and checkpoint lands on the node.
resource "aws_launch_template" "node" {
  for_each    = { standard = 100, gpu = 250, env = 100 }
  name_prefix = "${var.project}-${each.key}-"
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = each.value
      volume_type           = "gp3"
      iops                  = 6000
      throughput            = 400
      delete_on_termination = true
    }
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  user_data = each.key != "env" ? null : base64encode(<<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="BOUNDARY"

    --BOUNDARY
    Content-Type: application/node.eks.aws

    ---
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          maxPods: 110
    --BOUNDARY--
  EOT
  )
}
locals {
  private_ids = aws_subnet.private[*].id
  groups = {
    # Controllers, KEDA, Redis, the experiment tools.
    system = { instances = ["m5.xlarge"], count = 2, subnets = local.private_ids, ami = "AL2023_x86_64_STANDARD", lt = "standard", labels = { pool = "system" }, taint = null }
    # The sandbox workers of the environment tier.
    env = { instances = ["c6i.2xlarge"], count = var.env_nodes, subnets = local.private_ids, ami = "AL2023_x86_64_STANDARD", lt = "env", labels = { pool = "env" }, taint = null }
    # One GPU for the trainer. Tainted, so only pods that ask for it land here.
    trainer = { instances = var.trainer_instance_types, count = 1, subnets = [local.private_ids[var.gpu_az_index]], ami = "AL2023_x86_64_NVIDIA", lt = "gpu", labels = { pool = "trainer", gpu = "true" }, taint = "nvidia.com/gpu" }
    # The rollout engines: one GPU per node, in the same zone as the trainer.
    rollout = { instances = var.rollout_instance_types, count = var.rollout_nodes, subnets = local.private_ids, ami = "AL2023_x86_64_NVIDIA", lt = "gpu", labels = { pool = "rollout", gpu = "true" }, taint = "nvidia.com/gpu" }
  }
}
resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = local.groups.system.subnets
  instance_types  = local.groups.system.instances
  ami_type        = local.groups.system.ami
  labels          = local.groups.system.labels
  scaling_config {
    desired_size = local.groups.system.count
    min_size     = 0
    max_size     = local.groups.system.count + 2
  }
  launch_template {
    id      = aws_launch_template.node["standard"].id
    version = aws_launch_template.node["standard"].latest_version
  }
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni, aws_eks_addon.kube_proxy]
}
resource "aws_eks_node_group" "pool" {
  for_each        = { for k, v in local.groups : k => v if k != "system" }
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = each.value.subnets
  instance_types  = each.value.instances
  ami_type        = each.value.ami
  labels          = each.value.labels
  dynamic "taint" {
    for_each = each.value.taint == null ? [] : [each.value.taint]
    content {
      key    = taint.value
      value  = "present"
      effect = "NO_SCHEDULE"
    }
  }
  scaling_config {
    desired_size = each.value.count
    min_size     = 0
    max_size     = each.value.count + 2
  }
  launch_template {
    id      = aws_launch_template.node[each.value.lt].id
    version = aws_launch_template.node[each.value.lt].latest_version
  }
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni, aws_eks_addon.kube_proxy]
}
