# The components of Design 04 that are written for this implementation. They are one image run in different roles
# (`python -m agentplatform <role>`). Every one of them is stateless: state lives in the engine's database, the session
# database and S3.
locals {
  env_common = [
    { name = "DB_HOST", value = var.db_host },
    { name = "DB_PASSWORD", valueFrom = { secretKeyRef = { name = "db", key = "password" } } },
    { name = "TEMPORAL_ADDRESS", value = "temporal-frontend.temporal.svc:7233" },
    { name = "AGENT_IMAGE", value = var.agent_image },
    { name = "BUCKET", value = aws_s3_bucket.data.id },
    { name = "AWS_REGION", value = var.region },
  ]
  services = {
    model-gateway   = { ns = "agent-platform", replicas = 2, port = 8080, sa = null }
    tool-gateway    = { ns = "agent-platform", replicas = 2, port = 8080, sa = null }
    sandbox-manager = { ns = "agent-platform", replicas = 2, port = 8080, sa = "sandbox-manager" }
    session-api     = { ns = "agent-platform", replicas = 2, port = 8080, sa = null }
    git-host        = { ns = "external", replicas = 1, port = 8080, sa = null }
  }
  workers = {
    worker-v1 = { queue = "agent-v1", variant = "v1", replicas = 2 }
    worker-v2 = { queue = "agent-v2", variant = "v2-patched", replicas = 0 }
  }
}
resource "kubectl_manifest" "service_deployment" {
  for_each = local.services
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = each.key, namespace = each.value.ns }
    spec = {
      replicas = each.value.replicas
      selector = { matchLabels = { app = each.key } }
      template = {
        metadata = { labels = { app = each.key } }
        spec = {
          serviceAccountName = each.value.sa
          containers = [{
            name           = "main"
            image          = var.agent_image
            command        = ["python", "-m", "agentplatform", each.key]
            env            = local.env_common
            ports          = [{ containerPort = each.value.port }]
            resources      = { requests = { cpu = "100m", memory = "128Mi" } }
            readinessProbe = { httpGet = { path = "/healthz", port = each.value.port }, periodSeconds = 3 }
          }]
        }
      }
    }
  })
  depends_on = [kubernetes_namespace_v1.ns, kubernetes_secret_v1.db, kubernetes_service_account_v1.manager]
}
resource "kubernetes_service_v1" "service" {
  for_each = local.services
  metadata {
    name      = each.key
    namespace = each.value.ns
  }
  spec {
    selector = { app = each.key }
    port {
      port        = each.value.port
      target_port = each.value.port
    }
  }
  depends_on = [kubernetes_namespace_v1.ns]
}
# The workers. Stateless: any worker can continue any run. Spread over zones, and given time to finish what they are doing
# when they are told to stop.
resource "kubectl_manifest" "worker" {
  for_each = local.workers
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = each.key, namespace = "agent-platform" }
    spec = {
      replicas = each.value.replicas
      selector = { matchLabels = { app = each.key } }
      template = {
        metadata = {
          labels      = { app = each.key, role = "worker" }
          annotations = { "prometheus.io/scrape" = "true", "prometheus.io/port" = "9464", "prometheus.io/path" = "/metrics" }
        }
        spec = {
          terminationGracePeriodSeconds = 60
          topologySpreadConstraints = [{
            maxSkew       = 1, topologyKey = "topology.kubernetes.io/zone", whenUnsatisfiable = "ScheduleAnyway",
            labelSelector = { matchLabels = { app = each.key } }
          }]
          containers = [{
            name      = "worker"
            image     = var.agent_image
            command   = ["python", "-m", "agentplatform", "worker"]
            env       = concat(local.env_common, [{ name = "WORKER_QUEUE", value = each.value.queue }, { name = "WORKFLOW_VARIANT", value = each.value.variant }, { name = "GRACE_SECONDS", value = "50" }, { name = "ACTIVITY_SLOTS", value = "6" }])
            resources = { requests = { cpu = "200m", memory = "256Mi" } }
          }]
        }
      }
    }
  })
  depends_on = [helm_release.temporal, kubernetes_secret_v1.db]
  lifecycle { ignore_changes = [yaml_body] } # the scenarios scale and roll the workers; Terraform only creates them
}
# A pod the scenarios run their commands in: it has the same code and network position as any other client.
resource "kubectl_manifest" "tester" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "tester", namespace = "agent-platform" }
    spec = {
      replicas = 1
      selector = { matchLabels = { app = "tester" } }
      template = {
        metadata = { labels = { app = "tester" } }
        spec = {
          containers = [{
            name      = "main"
            image     = var.agent_image
            command   = ["sleep", "infinity"]
            env       = local.env_common
            resources = { requests = { cpu = "50m", memory = "128Mi" } }
          }]
        }
      }
    }
  })
  depends_on = [kubernetes_namespace_v1.ns, kubernetes_secret_v1.db]
}
# Workers scale on the engine's task queue backlog, not on CPU: most of a worker's time is spent waiting on model and tool calls.
# It is paused until Scenario S11 switches it on, so the other scenarios control the worker count themselves.
resource "kubectl_manifest" "worker_scaler" {
  yaml_body = yamlencode({
    apiVersion = "keda.sh/v1alpha1"
    kind       = "ScaledObject"
    metadata = {
      name        = "worker-v1"
      namespace   = "agent-platform"
      annotations = { "autoscaling.keda.sh/paused" = "true" }
    }
    spec = {
      scaleTargetRef  = { name = "worker-v1" }
      minReplicaCount = 2
      maxReplicaCount = 8
      triggers = [{
        type = "temporal"
        metadata = {
          namespace       = "default"
          taskQueue       = "agent-v1"
          targetQueueSize = "3"
          endpoint        = "temporal-frontend.temporal.svc:7233"
          queueTypes      = "workflow,activity"
        }
      }]
    }
  })
  depends_on = [helm_release.keda, kubectl_manifest.worker]
  lifecycle { ignore_changes = [yaml_body] }
}
