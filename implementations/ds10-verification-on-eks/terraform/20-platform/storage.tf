# One bucket: datasets (data/), the verifier wheels (wheels/), the verifier registry (registry/, written only by the
# operator's launcher), runs and their rollouts (runs/), fuzz reports (fuzz/), reviews, re-scoring and evaluations.
# Only the store service has an identity that reaches it.
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
  data_index = jsondecode(file("${var.data_dir}/index.json"))
}

resource "aws_s3_object" "data" {
  for_each = local.data_index
  bucket   = aws_s3_bucket.data.id
  key      = "data/${each.key}"
  source   = "${var.data_dir}/${each.key}"
  etag     = filemd5("${var.data_dir}/${each.key}")
}

resource "aws_s3_object" "index" {
  bucket = aws_s3_bucket.data.id
  key    = "data/index.json"
  source = "${var.data_dir}/index.json"
  etag   = filemd5("${var.data_dir}/index.json")
}

resource "aws_s3_object" "wheel" {
  for_each = var.wheels
  bucket   = aws_s3_bucket.data.id
  key      = "wheels/${each.key}"
  source   = "${var.wheels_dir}/${each.key}"
  etag     = filemd5("${var.wheels_dir}/${each.key}")
  lifecycle {
    precondition {
      condition     = filesha256("${var.wheels_dir}/${each.key}") == each.value
      error_message = "The downloaded wheel does not match its pinned sha256."
    }
  }
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

resource "aws_iam_role" "store" {
  name               = "${var.project}-store"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

# Reads everywhere in the bucket; writes only the prefixes the cluster produces. registry/ and data/ are not writable.
resource "aws_iam_role_policy" "store" {
  name = "store-access"
  role = aws_iam_role.store.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.data.arn}/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for p in ["runs", "fuzz", "reviews", "rescore", "evals", "gold"] : "${aws_s3_bucket.data.arn}/${p}/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn },
    ]
  })
}

resource "kubernetes_service_account_v1" "store" {
  metadata {
    name      = "store"
    namespace = "platform"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "aws_eks_pod_identity_association" "store" {
  cluster_name    = var.project
  namespace       = "platform"
  service_account = "store"
  role_arn        = aws_iam_role.store.arn
  depends_on      = [kubernetes_service_account_v1.store]
}

# A new association takes a few seconds to take effect, and a pod gets credentials only if it exists when it starts.
resource "time_sleep" "identity_ready" {
  create_duration = "45s"
  depends_on      = [aws_eks_pod_identity_association.store]
}
