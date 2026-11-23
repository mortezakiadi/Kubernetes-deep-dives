# Monitoring at the edge of the sandbox, in place of a runtime-security agent on the nodes. An agent on the node sees nothing
# inside a microVM, so this watches what crosses the boundary instead: every connection that leaves or enters the sandbox
# subnets (VPC Flow Logs) and every DNS question asked in the VPC (Route 53 Resolver query logs). Both go to CloudWatch Logs,
# and a subscription filter sends them on to the append-only audit store.
resource "aws_cloudwatch_log_group" "flow" {
  name              = "/${var.project}/vpc-flow-sandbox"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "dns" {
  name              = "/${var.project}/resolver-queries"
  retention_in_days = 30
}
data "aws_iam_policy_document" "flow_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}
data "aws_iam_policy_document" "flow_write" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}
resource "aws_iam_role" "flow" {
  name               = "${var.project}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_trust.json
}
resource "aws_iam_role_policy" "flow" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow_write.json
}
# One flow log per sandbox subnet, all traffic, one-minute aggregation.
resource "aws_flow_log" "sandbox" {
  for_each                 = toset(var.sandbox_subnet_ids)
  subnet_id                = each.value
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow.arn
  max_aggregation_interval = 60
}
resource "aws_route53_resolver_query_log_config" "dns" {
  name            = "${var.project}-resolver-queries"
  destination_arn = aws_cloudwatch_log_group.dns.arn
}
resource "aws_route53_resolver_query_log_config_association" "dns" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.dns.id
  resource_id                  = var.vpc_id
}
# Send both logs to the append-only store (see ../audit-store).
resource "aws_cloudwatch_log_subscription_filter" "flow" {
  name            = "${var.project}-flow-to-audit"
  log_group_name  = aws_cloudwatch_log_group.flow.name
  filter_pattern  = ""
  destination_arn = var.audit_firehose_arn
  role_arn        = var.logs_to_firehose_role_arn
}
resource "aws_cloudwatch_log_subscription_filter" "dns" {
  name            = "${var.project}-dns-to-audit"
  log_group_name  = aws_cloudwatch_log_group.dns.name
  filter_pattern  = ""
  destination_arn = var.audit_firehose_arn
  role_arn        = var.logs_to_firehose_role_arn
}
