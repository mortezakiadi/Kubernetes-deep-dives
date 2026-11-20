# Design 03, "Data and models": S3 is the durable tier for datasets, checkpoints and the model registry.
# Each family gets its own identity (EKS Pod Identity) scoped to the prefixes it needs.
resource "random_id" "bucket" {
  byte_length = 4
}
resource "aws_s3_bucket" "data" {
  bucket        = "${var.project}-data-${random_id.bucket.hex}"
  force_destroy = true # so teardown removes the objects with it
}
resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
# The training dataset: Tiny Shakespeare (public domain text, about 1.1 MB), published by Andrej Karpathy.
data "http" "shakespeare" {
  url = "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt"
}
resource "aws_s3_object" "shakespeare" {
  bucket  = aws_s3_bucket.data.id
  key     = "datasets/tinyshakespeare/input.txt"
  content = data.http.shakespeare.response_body
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
locals {
  bucket_arn = aws_s3_bucket.data.arn
  # name => { namespace, service account, read prefixes, read-write prefixes }
  identities = {
    trainer   = { ns = "llm-train", sa = "trainer", read = ["datasets"], write = ["runs"] }
    serving   = { ns = "inference", sa = "serving", read = ["registry"], write = [] }
    evaluator = { ns = "batch-eval", sa = "evaluator", read = ["registry"], write = ["evals", "batch"] }
    registry  = { ns = "platform", sa = "registry-ops", read = ["evals"], write = ["registry"] }
    notebook  = { ns = "research", sa = "notebook", read = ["datasets"], write = [] }
  }
}
resource "aws_iam_role" "identity" {
  for_each           = local.identities
  name               = "${var.project}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}
resource "aws_iam_role_policy" "identity" {
  for_each = local.identities
  name     = "prefix-access"
  role     = aws_iam_role.identity[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{ Effect = "Allow", Action = ["s3:GetObject"], Resource = [for p in concat(each.value.read, each.value.write) : "${local.bucket_arn}/${p}/*"] }],
      length(each.value.write) == 0 ? [] : [{ Effect = "Allow", Action = ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"], Resource = [for p in each.value.write : "${local.bucket_arn}/${p}/*"] }],
      [{ Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.bucket_arn, Condition = { StringLike = { "s3:prefix" = [for p in concat(each.value.read, each.value.write) : "${p}/*"] } } }],
    )
  })
}
resource "kubernetes_service_account_v1" "identity" {
  for_each = local.identities
  metadata {
    name      = each.value.sa
    namespace = each.value.ns
  }
  depends_on = [kubernetes_namespace_v1.family, kubernetes_namespace_v1.system]
}
resource "aws_eks_pod_identity_association" "identity" {
  for_each        = local.identities
  cluster_name    = var.project
  namespace       = each.value.ns
  service_account = each.value.sa
  role_arn        = aws_iam_role.identity[each.key].arn
  depends_on      = [kubernetes_service_account_v1.identity]
}
# A ConfigMap the workloads read, so no bucket name is ever hard-coded in a manifest.
resource "kubernetes_config_map_v1" "platform_env" {
  for_each = toset(concat([for f in local.families : f.ns], ["platform"]))
  metadata {
    name      = "platform-env"
    namespace = each.key
  }
  data = {
    BUCKET     = aws_s3_bucket.data.id
    AWS_REGION = var.region
  }
  depends_on = [kubernetes_namespace_v1.family, kubernetes_namespace_v1.system]
}
