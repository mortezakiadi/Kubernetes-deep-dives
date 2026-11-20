# Design 03, "Inference: the serving stack". Client -> Gateway -> (inference-aware endpoint picker) -> vLLM replica.
locals {
  model_name  = "qwen2.5-0.5b-instruct"
  served_name = "qwen2.5-0.5b"
  vllm_image  = "vllm/vllm-openai:${var.versions.vllm}"
}
# ---- the gateway and the endpoint picker ----------------------------------------------------------------------------
resource "helm_release" "agentgateway_crds" {
  name             = "agentgateway-crds"
  namespace        = "agentgateway-system"
  create_namespace = true
  repository       = "oci://cr.agentgateway.dev/charts"
  chart            = "agentgateway-crds"
  version          = var.versions.agentgateway
  wait             = true
}
resource "helm_release" "agentgateway" {
  name       = "agentgateway"
  namespace  = "agentgateway-system"
  repository = "oci://cr.agentgateway.dev/charts"
  chart      = "agentgateway"
  version    = var.versions.agentgateway
  wait       = true
  timeout    = 600
  set        = [{ name = "inferenceExtension.enabled", value = "true" }]
  depends_on = [helm_release.agentgateway_crds]
}
resource "kubectl_manifest" "gateway_params" {
  yaml_body = yamlencode({
    apiVersion = "agentgateway.dev/v1alpha1"
    kind       = "AgentgatewayParameters"
    metadata   = { name = "internal", namespace = "inference-gw" }
    spec       = { service = { spec = { type = "ClusterIP" } } }
  })
  depends_on = [helm_release.agentgateway, kubernetes_namespace_v1.system]
}
resource "kubectl_manifest" "gateway" {
  yaml_body = yamlencode({
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "Gateway"
    metadata   = { name = "inference-gateway", namespace = "inference-gw" }
    spec = {
      gatewayClassName = "agentgateway"
      infrastructure   = { parametersRef = { name = "internal", group = "agentgateway.dev", kind = "AgentgatewayParameters" } }
      listeners        = [{ name = "http", port = 80, protocol = "HTTP", allowedRoutes = { namespaces = { from = "All" } } }]
    }
  })
  depends_on = [kubectl_manifest.gateway_params]
}
resource "helm_release" "router" {
  name       = "qwen-router"
  namespace  = "inference"
  repository = "oci://ghcr.io/llm-d/charts"
  chart      = "llm-d-router-gateway"
  version    = var.versions.llmd_router
  wait       = true
  timeout    = 600
  values = [yamlencode({
    router = {
      modelServers = { matchLabels = { app = "vllm-qwen" } }
      epp = {
        resources = { requests = { cpu = "100m", memory = "128Mi" }, limits = { memory = "512Mi" } }
      }
    }
    provider  = { name = "none" }
    httpRoute = { create = true, inferenceGatewayName = "inference-gateway", inferenceGatewayNamespace = "inference-gw", requestTimeout = "300s" }
  })]
  depends_on = [kubectl_manifest.gateway, kubernetes_namespace_v1.family]
}
# ---- the model server -------------------------------------------------------------------------------------------------
resource "kubectl_manifest" "vllm" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = "vllm-qwen"
      namespace = "inference"
      labels = {
        app                             = "vllm-qwen"
        "kueue.x-k8s.io/queue-name"     = "serving"
        "kueue.x-k8s.io/priority-class" = "production"
      }
    }
    spec = {
      replicas = 0 # the tests bring it up once a version has been promoted
      selector = { matchLabels = { app = "vllm-qwen" } }
      strategy = { type = "RollingUpdate", rollingUpdate = { maxUnavailable = 1, maxSurge = 0 } }
      template = {
        metadata = {
          labels      = { app = "vllm-qwen", "kueue.x-k8s.io/priority-class" = "production" }
          annotations = { "prometheus.io/scrape" = "true", "prometheus.io/port" = "8000", "prometheus.io/path" = "/metrics" }
        }
        spec = {
          serviceAccountName            = "serving"
          priorityClassName             = "serving-high"
          enableServiceLinks            = false
          terminationGracePeriodSeconds = 60
          # Replicas spread over zones so one zone's trouble does not take the service down.
          topologySpreadConstraints = [{
            maxSkew           = 1
            topologyKey       = "topology.kubernetes.io/zone"
            whenUnsatisfiable = "ScheduleAnyway"
            labelSelector     = { matchLabels = { app = "vllm-qwen" } }
          }]
          initContainers = [{
            name    = "fetch-weights"
            image   = "public.ecr.aws/aws-cli/aws-cli:2.27.0"
            command = ["/bin/sh", "-c"]
            args = [<<-SH
              set -e
              V=$(aws s3 cp s3://$BUCKET/registry/models/${local.model_name}/pointers/production - )
              echo "production pointer names $V"
              aws s3 sync s3://$BUCKET/registry/models/${local.model_name}/$V/ /models/current --only-show-errors
              echo "$V" > /models/current/VERSION
              ls -la /models/current | head -20
            SH
            ]
            envFrom      = [{ configMapRef = { name = "platform-env" } }]
            volumeMounts = [{ name = "models", mountPath = "/models" }]
          }]
          containers = [{
            name  = "vllm"
            image = local.vllm_image
            args = [
              "/models/current", "--served-model-name", local.served_name, "--port", "8000",
              "--max-model-len", "8192", "--max-num-seqs", "8", "--gpu-memory-utilization", "0.85", "--enable-prefix-caching",
            ]
            ports = [{ name = "http", containerPort = 8000 }]
            resources = {
              requests = { cpu = "3", memory = "12Gi", "nvidia.com/gpu" = "1" }
              limits   = { "nvidia.com/gpu" = "1" }
            }
            startupProbe   = { httpGet = { path = "/health", port = "http" }, periodSeconds = 5, failureThreshold = 240 }
            readinessProbe = { httpGet = { path = "/health", port = "http" }, periodSeconds = 2, failureThreshold = 1 }
            livenessProbe  = { httpGet = { path = "/health", port = "http" }, periodSeconds = 10, failureThreshold = 6 }
            volumeMounts   = [{ name = "models", mountPath = "/models" }, { name = "shm", mountPath = "/dev/shm" }]
          }]
          volumes = [
            { name = "models", emptyDir = {} },
            { name = "shm", emptyDir = { medium = "Memory", sizeLimit = "2Gi" } },
          ]
        }
      }
    }
  })
  depends_on = [helm_release.kueue, kubectl_manifest.local_queue, kubernetes_service_account_v1.identity]
  lifecycle { ignore_changes = [yaml_body] } # the tests scale it and roll it; Terraform only creates it
}
# A plain Service over the same pods. The tests use it to show what round-robin routing does without the picker.
resource "kubernetes_service_v1" "vllm_direct" {
  metadata {
    name      = "vllm-direct"
    namespace = "inference"
  }
  spec {
    selector = { app = "vllm-qwen" }
    port {
      port        = 8000
      target_port = 8000
    }
  }
  depends_on = [kubernetes_namespace_v1.family]
}
# ---- warm buffer: placeholder pods that hold an idle GPU node (and the serving image) ready -------------------------
resource "kubectl_manifest" "buffer" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "gpu-buffer", namespace = "gpu-buffer" }
    spec = {
      replicas = 0
      selector = { matchLabels = { app = "gpu-buffer" } }
      template = {
        metadata = { labels = { app = "gpu-buffer" } }
        spec = {
          priorityClassName             = "buffer-low"
          nodeSelector                  = { "ds03/pool" = "inference" }
          tolerations                   = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
          terminationGracePeriodSeconds = 0
          containers = [{
            name      = "hold"
            image     = local.vllm_image # the same image, so it is already on the node when the real replica arrives
            command   = ["sleep", "infinity"]
            resources = { requests = { cpu = "100m", memory = "128Mi", "nvidia.com/gpu" = "1" }, limits = { "nvidia.com/gpu" = "1" } }
          }]
        }
      }
    }
  })
  depends_on = [kubernetes_priority_class_v1.buffer, kubernetes_namespace_v1.system]
  lifecycle { ignore_changes = [yaml_body] }
}
# ---- autoscaling on serving signals, not on CPU or GPU utilization ----------------------------------------------------
resource "kubectl_manifest" "scaledobject" {
  yaml_body = yamlencode({
    apiVersion = "keda.sh/v1alpha1"
    kind       = "ScaledObject"
    metadata = {
      name        = "vllm-qwen"
      namespace   = "inference"
      annotations = { "autoscaling.keda.sh/paused" = "true" } # the autoscaling scenario switches it on
    }
    spec = {
      scaleTargetRef  = { name = "vllm-qwen" }
      minReplicaCount = 1
      maxReplicaCount = 2
      triggers = [{
        type = "prometheus"
        metadata = {
          serverAddress = "http://prometheus-server.monitoring.svc"
          query         = "sum(vllm:num_requests_waiting{namespace=\"inference\"})"
          threshold     = "4"
        }
      }]
    }
  })
  depends_on = [helm_release.keda, helm_release.prometheus, kubectl_manifest.vllm]
  lifecycle { ignore_changes = [yaml_body] }
}
# ---- GPU telemetry -------------------------------------------------------------------------------------------------------
resource "helm_release" "dcgm" {
  name       = "dcgm-exporter"
  namespace  = "monitoring"
  repository = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart      = "dcgm-exporter"
  version    = var.versions.dcgm
  wait       = false
  values = [yamlencode({
    tolerations    = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    nodeSelector   = { "eks.amazonaws.com/instance-gpu-manufacturer" = "nvidia" }
    serviceMonitor = { enabled = false }
    podAnnotations = { "prometheus.io/scrape" = "true", "prometheus.io/port" = "9400" }
  })]
  depends_on = [kubernetes_namespace_v1.system]
}
