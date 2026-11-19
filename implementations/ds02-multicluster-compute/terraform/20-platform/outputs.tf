output "bucket" {
  value = aws_s3_bucket.data.id
}

output "clusters" {
  value = { for k, v in local.eks : k => v.name }
}
