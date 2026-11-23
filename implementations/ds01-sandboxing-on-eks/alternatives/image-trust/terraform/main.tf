# Image trust: a private mirror of the two public registries the sandboxes pull from, a signing key, and the permissions a build
# job needs to warm the mirror and sign what it finds. Nodes keep their pull-only role. The admission check is in ../kyverno-policy.yaml.
resource "aws_ecr_pull_through_cache_rule" "ecr_public" {
  ecr_repository_prefix = "ecr-public"
  upstream_registry_url = "public.ecr.aws"
}
resource "aws_ecr_pull_through_cache_rule" "kubernetes" {
  ecr_repository_prefix = "k8s"
  upstream_registry_url = "registry.k8s.io"
}
# Tags in the mirrored repositories cannot be moved once pushed.
resource "aws_ecr_repository_creation_template" "mirror" {
  for_each             = toset(["ecr-public", "k8s"])
  prefix               = each.value
  applied_for          = ["PULL_THROUGH_CACHE"]
  image_tag_mutability = "IMMUTABLE"
}
# The signing key never leaves KMS. cosign signs with it through the URI awskms:///alias/<project>-image-signing.
resource "aws_kms_key" "image_signing" {
  description              = "${var.project} image signing"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
}
resource "aws_kms_alias" "image_signing" {
  name          = "alias/${var.project}-image-signing"
  target_key_id = aws_kms_key.image_signing.key_id
}
# What the build job needs: import from the upstream registries, store the signature next to the image, and sign with the key.
data "aws_iam_policy_document" "warm_and_sign" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:CreateRepository", "ecr:BatchImportUpstreamImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability", "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
    ]
    resources = [
      "arn:aws:ecr:${var.region}:${var.account_id}:repository/ecr-public/*",
      "arn:aws:ecr:${var.region}:${var.account_id}:repository/k8s/*",
    ]
  }
  statement {
    actions   = ["kms:Sign", "kms:GetPublicKey", "kms:DescribeKey"]
    resources = [aws_kms_key.image_signing.arn]
  }
}
resource "aws_iam_policy" "warm_and_sign" {
  name   = "${var.project}-image-warm-and-sign"
  policy = data.aws_iam_policy_document.warm_and_sign.json
}
output "signing_key_uri" {
  value = "awskms:///${aws_kms_alias.image_signing.name}"
}
output "mirror_registry" {
  value = "${var.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}
