output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "region" {
  value = var.region
}

output "sandbox_node_role_name" {
  value = aws_iam_role.sandbox_node.name
}

output "sandbox_security_group_id" {
  value = aws_security_group.sandbox_nodes.id
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --name ${aws_eks_cluster.this.name} --region ${var.region} --kubeconfig ../../.kube/config"
}
