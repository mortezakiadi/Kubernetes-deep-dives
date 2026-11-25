output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "region" {
  value = var.region
}

output "project" {
  value = var.project
}

output "vpc_cidr" {
  value = var.vpc_cidr
}
