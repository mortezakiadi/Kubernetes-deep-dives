# Identity per team (Design 02: "each team's workloads use EKS Pod Identity roles scoped to their own S3
# prefixes"). One bucket, one prefix per team, one IAM role per team, associated with the team's job
# ServiceAccount in every worker cluster.

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

resource "aws_s3_object" "team_file" {
  for_each = local.teams
  bucket   = aws_s3_bucket.data.id
  key      = "${each.key}/dataset.txt"
  content  = "dataset owned by team ${each.key}\n"
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

resource "aws_iam_role" "team" {
  for_each           = local.teams
  name               = "${var.project}-team-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy" "team" {
  for_each = local.teams
  name     = "own-prefix-read"
  role     = aws_iam_role.team[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.data.arn}/${each.key}/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn, Condition = { StringLike = { "s3:prefix" = ["${each.key}/*"] } } },
    ]
  })
}

locals {
  identity_pairs = { for p in setproduct(["cpu-a", "cpu-b", "gpu"], keys(local.teams)) : "${p[0]}/${p[1]}" => { cluster = p[0], team = p[1] } }
}

resource "aws_eks_pod_identity_association" "team" {
  for_each        = local.identity_pairs
  cluster_name    = local.eks[each.value.cluster].name
  namespace       = each.value.team
  service_account = "team-job"
  role_arn        = aws_iam_role.team[each.value.team].arn
  depends_on      = [module.cpu_a, module.cpu_b, module.gpu]
}
