output "cluster_name" {
  value = aws_eks_cluster.this.name
}
output "region" {
  value = var.region
}
output "project" {
  value = var.project
}
output "db_host" {
  value = aws_db_instance.this.address
}
output "db_identifier" {
  value = aws_db_instance.this.identifier
}
output "db_password" {
  value     = random_password.db.result
  sensitive = true
}
output "ecr_repository" {
  value = aws_ecr_repository.agent.repository_url
}
