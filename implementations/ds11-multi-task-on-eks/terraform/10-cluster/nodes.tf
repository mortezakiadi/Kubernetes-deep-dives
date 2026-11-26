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
  for_each    = { standard = 100, gpu = 200 }
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
}
locals {
  private_ids = aws_subnet.private[*].id
  groups = {
    # Controllers (Kueue, the device plugin's helper pods) and the experiment tools.
    system = { instances = ["m5.xlarge"], count = 2, subnets = local.private_ids, ami = "AL2023_x86_64_STANDARD", lt = "standard", labels = { pool = "system" }, taint = null, max = 4 }
    # The experiment GPUs. Tainted, so only pods that ask for a GPU land here.
    research = { instances = var.research_instance_types, count = var.research_nodes, subnets = local.private_ids, ami = "AL2023_x86_64_NVIDIA", lt = "gpu", labels = { pool = "research", gpu = "true" }, taint = "nvidia.com/gpu", max = 4 }
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
    max_size     = local.groups.system.max
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
    max_size     = each.value.max
  }
  launch_template {
    id      = aws_launch_template.node[each.value.lt].id
    version = aws_launch_template.node[each.value.lt].latest_version
  }
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni, aws_eks_addon.kube_proxy]
}
