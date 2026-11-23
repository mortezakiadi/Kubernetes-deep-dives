# One bucket, the trajectory store's backing: trajectories, snapshots, per-turn contexts and log-probabilities, credit
# records, and the public half of the task bank. Only the trajectory store service has access to it.
resource "random_id" "bucket" {
  byte_length = 4
}

resource "aws_s3_bucket" "data" {
  bucket        = "${var.project}-data-${random_id.bucket.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

locals {
  task_index = jsondecode(file("${var.tasks_dir}/index.json"))
}

# The public half of each task (files and issue). The hidden half never goes to the bucket: it is in the verifier's
# ConfigMap only.
resource "aws_s3_object" "task" {
  for_each = local.task_index
  bucket   = aws_s3_bucket.data.id
  key      = "tasks/public/${each.key}.json"
  source   = "${var.tasks_dir}/public/${each.key}.json"
  etag     = filemd5("${var.tasks_dir}/public/${each.key}.json")
}

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trajstore" {
  name               = "${var.project}-trajstore"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy" "trajstore" {
  name = "store-access"
  role = aws_iam_role.trajstore.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.data.arn}/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for p in ["trajectories", "snapshots", "contexts", "logprobs", "credit", "curriculum"] : "${aws_s3_bucket.data.arn}/${p}/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn },
    ]
  })
}

resource "kubernetes_service_account_v1" "trajstore" {
  metadata {
    name      = "trajstore"
    namespace = "platform"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "aws_eks_pod_identity_association" "trajstore" {
  cluster_name    = var.project
  namespace       = "platform"
  service_account = "trajstore"
  role_arn        = aws_iam_role.trajstore.arn
  depends_on      = [kubernetes_service_account_v1.trajstore]
}

# A new association takes a few seconds to take effect, and a pod gets credentials only if it exists when it starts.
resource "time_sleep" "identity_ready" {
  create_duration = "45s"
  depends_on      = [aws_eks_pod_identity_association.trajstore]
}
