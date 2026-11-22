# Checkpoints are written to the node first and copied to this bucket in the background (Design 04's recovery rule: the node is the fast copy,
# the bucket is the one that survives the node). The same bucket holds the experiment logs the tests read.
resource "random_id" "bucket" {
  byte_length = 4
}
resource "aws_s3_bucket" "state" {
  bucket        = "${var.project}-state-${random_id.bucket.hex}"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_iam_role" "workload" {
  name = "${var.project}-workload"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}
resource "aws_iam_role_policy" "workload" {
  name = "state-bucket"
  role = aws_iam_role.workload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.state.arn}/*" },
    ]
  })
}
resource "aws_eks_pod_identity_association" "workload" {
  for_each        = toset(["trainer", "tools"])
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "rl-scale"
  service_account = each.value
  role_arn        = aws_iam_role.workload.arn
}
