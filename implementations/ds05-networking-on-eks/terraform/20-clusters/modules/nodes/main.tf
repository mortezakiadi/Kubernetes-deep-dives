# The managed node group. Nodes are created after the per-zone ENIConfigs exist, because with custom networking a node cannot give a pod an address without one.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "cluster_name" { type = string }
variable "name" { type = string }
variable "groups" {
  description = "One managed node group per entry: its subnets, the ENIConfig its nodes use, and how many nodes."
  type = map(object({
    subnet_ids = list(string)
    eniconfig  = string
    desired    = number
  }))
}
variable "instance_type" {
  type    = string
  default = "m5.large"
}
variable "cluster_security_group_id" { type = string }

resource "aws_iam_role" "node" {
  name = "${var.name}-node"
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

# With prefix delegation a node can run far more pods than the default calculation allows, so the limit is set explicitly.
resource "aws_launch_template" "node" {
  name_prefix            = "${var.name}-"
  vpc_security_group_ids = [var.cluster_security_group_id]
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  user_data = base64encode(<<-EOT
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

resource "aws_eks_node_group" "this" {
  for_each        = var.groups
  cluster_name    = var.cluster_name
  node_group_name = "${var.name}-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = each.value.subnet_ids
  instance_types  = [var.instance_type]
  labels          = { eniconfig = each.value.eniconfig }
  scaling_config {
    desired_size = each.value.desired
    min_size     = 0
    max_size     = each.value.desired + 2
  }
  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }
  lifecycle { ignore_changes = [scaling_config[0].desired_size] }
  depends_on = [aws_iam_role_policy_attachment.node]
}
output "node_role_arn" { value = aws_iam_role.node.arn }
