# An append-only audit store. Every platform pod already writes one JSON line per event with the session ID. The cluster's log
# agent ships those lines to CloudWatch Logs. From there a subscription filter sends them through Kinesis Data Firehose to an S3
# bucket with Object Lock in compliance mode: for the retention period nobody, including the account root user, can delete or
# overwrite an object. The audit trail is then a record you can show to a customer, not only a log that an operator can edit.
resource "aws_kms_key" "audit" {
  description             = "${var.project} audit store"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_s3_bucket" "audit" {
  bucket              = "${var.project}-audit-${var.account_id}"
  object_lock_enabled = true
}
resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}
resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}
resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit.arn
    }
  }
}
resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
data "aws_iam_policy_document" "audit_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit_bucket.json
}
# --- Firehose: CloudWatch Logs in, S3 out --------------------------------------------------------------------------------------
data "aws_iam_policy_document" "firehose_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }
  }
}
data "aws_iam_policy_document" "firehose_to_s3" {
  statement {
    actions   = ["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.audit.arn]
  }
}
resource "aws_iam_role" "firehose" {
  name               = "${var.project}-audit-firehose"
  assume_role_policy = data.aws_iam_policy_document.firehose_trust.json
}
resource "aws_iam_role_policy" "firehose" {
  name   = "audit-to-s3"
  role   = aws_iam_role.firehose.id
  policy = data.aws_iam_policy_document.firehose_to_s3.json
}
resource "aws_kinesis_firehose_delivery_stream" "audit" {
  name        = "${var.project}-audit"
  destination = "extended_s3"
  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.audit.arn
    prefix              = "audit/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "audit-errors/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    buffering_size      = 5
    buffering_interval  = 60
    kms_key_arn         = aws_kms_key.audit.arn
  }
}
# --- CloudWatch Logs to Firehose ------------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "logs_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["logs.amazonaws.com"]
    }
  }
}
data "aws_iam_policy_document" "logs_to_firehose" {
  statement {
    actions   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
    resources = [aws_kinesis_firehose_delivery_stream.audit.arn]
  }
}
resource "aws_iam_role" "logs_to_firehose" {
  name               = "${var.project}-audit-logs-to-firehose"
  assume_role_policy = data.aws_iam_policy_document.logs_trust.json
}
resource "aws_iam_role_policy" "logs_to_firehose" {
  name   = "put-to-firehose"
  role   = aws_iam_role.logs_to_firehose.id
  policy = data.aws_iam_policy_document.logs_to_firehose.json
}
# The log group that the cluster's log agent writes the platform pods' output to.
resource "aws_cloudwatch_log_subscription_filter" "platform" {
  name            = "${var.project}-audit"
  log_group_name  = var.platform_log_group_name
  filter_pattern  = "\"session_id\""
  destination_arn = aws_kinesis_firehose_delivery_stream.audit.arn
  role_arn        = aws_iam_role.logs_to_firehose.arn
}
output "firehose_arn" {
  value = aws_kinesis_firehose_delivery_stream.audit.arn
}
output "logs_to_firehose_role_arn" {
  value = aws_iam_role.logs_to_firehose.arn
}
output "bucket" {
  value = aws_s3_bucket.audit.bucket
}
