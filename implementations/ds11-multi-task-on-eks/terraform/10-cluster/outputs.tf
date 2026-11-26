output "cluster" {
  value = {
    name     = aws_eks_cluster.this.name
    region   = var.region
    endpoint = aws_eks_cluster.this.endpoint
    ca_b64   = aws_eks_cluster.this.certificate_authority[0].data
  }
}
output "state_bucket" { value = aws_s3_bucket.state.bucket }
output "vpc_id" { value = aws_vpc.this.id }
