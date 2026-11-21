# One standard EKS cluster (managed control plane, managed node group, add-ons). Standard, not Auto Mode, because Design 05 is about the
# settings Auto Mode takes away: custom pod networking, IP allocation, network policy agent, and an alternative CNI next to the VPC CNI.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "name" { type = string }
variable "kubernetes_version" { type = string }
variable "node_subnet_ids" { type = list(string) }
variable "admin_cidr" { type = string }
variable "network_policy" {
  description = "Run the VPC CNI's own network policy agent. Off on the cluster where Cilium enforces policy."
  type        = bool
  default     = true
}

resource "aws_iam_role" "cluster" {
  name = "${var.name}-cluster"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version
  vpc_config {
    subnet_ids              = var.node_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.admin_cidr]
  }
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }
  enabled_cluster_log_types = ["audit"]
  depends_on                = [aws_iam_role_policy_attachment.cluster]
}

# The VPC CNI is configured the way the design says:
#   custom networking   pods take addresses from their own subnets (100.64.0.0/16), chosen by an ENIConfig per zone
#   prefix delegation   a node takes /28 blocks instead of single addresses
#   SNAT left on        a pod that leaves the VPC is translated to its node's routable address
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  configuration_values = jsonencode({
    enableNetworkPolicy = tostring(var.network_policy)
    env = {
      AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true"
      ENI_CONFIG_LABEL_DEF               = "eniconfig"
      ENABLE_PREFIX_DELEGATION           = "true"
      WARM_PREFIX_TARGET                 = "1"
    }
  })
}

output "name" { value = aws_eks_cluster.this.name }
output "endpoint" { value = aws_eks_cluster.this.endpoint }
output "ca_b64" { value = aws_eks_cluster.this.certificate_authority[0].data }
output "cluster_security_group_id" { value = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id }
output "oidc_issuer" { value = aws_eks_cluster.this.identity[0].oidc[0].issuer }
output "vpc_cni_addon" { value = aws_eks_addon.vpc_cni.id }
