output "cluster_names" {
  value = { for k, v in aws_eks_cluster.this : k => v.name }
}

output "region" {
  value = var.region
}

output "project" {
  value = var.project
}
