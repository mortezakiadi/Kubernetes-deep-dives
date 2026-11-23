# Shared state for the tool gateway: one DynamoDB table, a role that can only read and write that table,
# an EKS Pod Identity association that gives the role to the tool gateway's service account, and a gateway
# endpoint so DynamoDB traffic stays inside the VPC.
resource "aws_dynamodb_table" "tool_gateway" {
  name         = "${var.project}-tool-gateway"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
}
data "aws_iam_policy_document" "tool_gateway_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}
data "aws_iam_policy_document" "tool_gateway_table" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.tool_gateway.arn]
  }
}
resource "aws_iam_role" "tool_gateway" {
  name               = "${var.project}-tool-gateway"
  assume_role_policy = data.aws_iam_policy_document.tool_gateway_trust.json
}
resource "aws_iam_role_policy" "tool_gateway" {
  name   = "tool-gateway-table"
  role   = aws_iam_role.tool_gateway.id
  policy = data.aws_iam_policy_document.tool_gateway_table.json
}
resource "aws_eks_pod_identity_association" "tool_gateway" {
  cluster_name    = var.cluster_name
  namespace       = "sandbox-platform"
  service_account = "tool-gateway"
  role_arn        = aws_iam_role.tool_gateway.arn
}
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids
}
output "table_name" {
  value = aws_dynamodb_table.tool_gateway.name
}
