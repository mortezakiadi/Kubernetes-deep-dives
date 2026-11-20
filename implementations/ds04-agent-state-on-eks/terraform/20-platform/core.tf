# ---- namespaces ---------------------------------------------------------------------------------------------------
locals {
  namespaces = ["temporal", "agent-platform", "sandboxes", "external", "monitoring"]
}
resource "kubernetes_namespace_v1" "ns" {
  for_each = toset(local.namespaces)
  metadata {
    name = each.key
    labels = {
      "pod-security.kubernetes.io/enforce" = "baseline"
      "pod-security.kubernetes.io/warn"    = "baseline"
    }
  }
}
# The database password, as a Secret in each namespace that needs it. It never appears in a manifest.
resource "kubernetes_secret_v1" "db" {
  for_each = toset(["temporal", "agent-platform", "external"])
  metadata {
    name      = "db"
    namespace = each.key
  }
  data       = { password = var.db_password }
  depends_on = [kubernetes_namespace_v1.ns]
}
# ---- S3 and the sandbox identity --------------------------------------------------------------------------------------
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
data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}
# A sandbox may read and write workspace snapshots and nothing else in the account.
resource "aws_iam_role" "sandbox" {
  name               = "${var.project}-sandbox"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}
resource "aws_iam_role_policy" "sandbox" {
  name = "snapshots-only"
  role = aws_iam_role.sandbox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.data.arn}/snapshots/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn, Condition = { StringLike = { "s3:prefix" = ["snapshots/*"] } } },
    ]
  })
}
resource "kubernetes_service_account_v1" "sandbox" {
  metadata {
    name      = "sandbox"
    namespace = "sandboxes"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}
resource "aws_eks_pod_identity_association" "sandbox" {
  cluster_name    = var.project
  namespace       = "sandboxes"
  service_account = "sandbox"
  role_arn        = aws_iam_role.sandbox.arn
  depends_on      = [kubernetes_service_account_v1.sandbox]
}
# The sandbox manager creates and deletes sandbox pods, in one namespace only.
resource "kubernetes_service_account_v1" "manager" {
  metadata {
    name      = "sandbox-manager"
    namespace = "agent-platform"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}
resource "kubernetes_role_v1" "manager" {
  metadata {
    name      = "sandbox-manager"
    namespace = "sandboxes"
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["create", "get", "list", "delete"]
  }
  depends_on = [kubernetes_namespace_v1.ns]
}
resource "kubernetes_role_binding_v1" "manager" {
  metadata {
    name      = "sandbox-manager"
    namespace = "sandboxes"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.manager.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "sandbox-manager"
    namespace = "agent-platform"
  }
}
# ---- the durable execution engine --------------------------------------------------------------------------------------
# Temporal, self-hosted, on the Multi-AZ PostgreSQL instance. Two replicas of each stateful service, so losing one pod
# is survivable (Scenario S10). The history shard count is fixed for the life of the cluster; 128 is plenty at this scale.
resource "helm_release" "temporal" {
  name       = "temporal"
  namespace  = "temporal"
  repository = "https://go.temporal.io/helm-charts"
  chart      = "temporal"
  version    = var.versions.temporal_chart
  wait       = true
  timeout    = 900
  values = [yamlencode({
    server = {
      replicaCount = 2
      worker       = { replicaCount = 1 }
      config = {
        logLevel   = "info"
        namespaces = { create = true, useHelmHooks = false, namespace = [{ name = "default", retention = "3d" }] }
        persistence = {
          defaultStore     = "default"
          visibilityStore  = "visibility"
          numHistoryShards = 128
          datastores = {
            default = { sql = {
              pluginName = "postgres12", driverName = "postgres12", databaseName = "temporal", connectAddr = "${var.db_host}:5432", connectProtocol = "tcp",
            user = "temporal", existingSecret = "db", secretKey = "password", createDatabase = true, manageSchema = true, maxConns = 20, maxIdleConns = 20, maxConnLifetime = "1h" } }
            visibility = { sql = {
              pluginName = "postgres12", driverName = "postgres12", databaseName = "temporal_visibility", connectAddr = "${var.db_host}:5432", connectProtocol = "tcp",
            user = "temporal", existingSecret = "db", secretKey = "password", createDatabase = true, manageSchema = true, maxConns = 10, maxIdleConns = 10, maxConnLifetime = "1h" } }
          }
        }
      }
      resources = { requests = { cpu = "200m", memory = "512Mi" } }
    }
    schema     = { useHelmHooks = false }
    web        = { enabled = true, resources = { requests = { cpu = "50m", memory = "64Mi" } } }
    admintools = { enabled = true }
  })]
  depends_on = [kubernetes_secret_v1.db]
}
# ---- metrics and autoscaling ---------------------------------------------------------------------------------------------
resource "helm_release" "prometheus" {
  name       = "prometheus"
  namespace  = "monitoring"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = var.versions.prometheus
  wait       = true
  timeout    = 600
  values = [yamlencode({
    alertmanager             = { enabled = false }
    prometheus-pushgateway   = { enabled = false }
    prometheus-node-exporter = { enabled = false }
    kube-state-metrics       = { enabled = true }
    server = {
      persistentVolume = { enabled = false }
      retention        = "6h"
      global           = { scrape_interval = "10s", evaluation_interval = "10s" }
    }
  })]
  depends_on = [kubernetes_namespace_v1.ns]
}
resource "helm_release" "keda" {
  name             = "keda"
  namespace        = "keda"
  create_namespace = true
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.versions.keda
  wait             = true
  timeout          = 600
}
