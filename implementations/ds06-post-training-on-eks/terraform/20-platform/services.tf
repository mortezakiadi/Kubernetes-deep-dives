# ---- code and settings the jobs read -----------------------------------------------------------------------------------
# The workload code ships as a ConfigMap, so the image stays the unmodified, pinned vLLM release. The launcher hashes
# this ConfigMap and records the hash with every run.
locals {
  code = { for f in fileset("${path.module}/../../workloads", "*.{py,sh}") : f => file("${path.module}/../../workloads/${f}") }
  env = {
    BUCKET       = aws_s3_bucket.data.id
    AWS_REGION   = var.region
    VERIFIER_URL = "http://verifier.rewards.svc.cluster.local:8080"
  }
}

resource "kubernetes_config_map_v1" "code" {
  for_each = toset(["post-training", "platform"])
  metadata {
    name      = "workload-code"
    namespace = each.key
  }
  data       = local.code
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_config_map_v1" "env" {
  for_each = toset(["post-training", "platform"])
  metadata {
    name      = "platform-env"
    namespace = each.key
  }
  data       = local.env
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the reward verifier pool (Design 06, "Rewards and verifiers") -----------------------------------------------------
resource "kubernetes_config_map_v1" "verifier" {
  metadata {
    name      = "verifier-code"
    namespace = "rewards"
  }
  data       = { "verifier.py" = file("${path.module}/../../workloads/verifier.py") }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_deployment_v1" "verifier" {
  metadata {
    name      = "verifier"
    namespace = "rewards"
    labels    = { app = "verifier" }
  }
  spec {
    replicas = 2
    selector { match_labels = { app = "verifier" } }
    template {
      metadata {
        labels      = { app = "verifier" }
        annotations = { "ds06/code-sha" = sha256(file("${path.module}/../../workloads/verifier.py")) }
      }
      spec {
        automount_service_account_token = false # no Kubernetes credentials for code that runs model output
        node_selector                   = { "ds06/pool" = "system" }
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = "verifier"
          image   = var.python_image
          command = ["python", "-u", "/code/verifier.py"]
          env {
            name  = "CODE_SLOTS"
            value = "4"
          }
          port { container_port = 8080 }
          resources {
            requests = { cpu = "500m", memory = "512Mi" }
            limits   = { cpu = "2", memory = "2Gi" }
          }
          security_context {
            run_as_non_root            = true # set again here: the provider otherwise writes false at container level
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          # exec probe: traffic from localhost is not subject to the namespace's NetworkPolicy
          readiness_probe {
            exec { command = ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"] }
            period_seconds = 10
          }
          volume_mount {
            name       = "code"
            mount_path = "/code"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "code"
          config_map { name = "verifier-code" }
        }
        volume {
          name = "tmp"
          empty_dir { size_limit = "1Gi" }
        }
      }
    }
  }
  depends_on = [kubernetes_config_map_v1.verifier]
}

resource "kubernetes_service_v1" "verifier" {
  metadata {
    name      = "verifier"
    namespace = "rewards"
  }
  spec {
    selector = { app = "verifier" }
    port {
      port        = 8080
      target_port = 8080
    }
  }
}

# Sized so reward never becomes the bottleneck: more verifier pods when their CPU is busy.
resource "kubernetes_horizontal_pod_autoscaler_v2" "verifier" {
  metadata {
    name      = "verifier"
    namespace = "rewards"
  }
  spec {
    min_replicas = 2
    max_replicas = 6
    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = "verifier"
    }
    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = 60
        }
      }
    }
  }
}

# No network for the verifiers: nothing leaves the namespace (not even DNS), and only the run namespaces may call in.
resource "kubectl_manifest" "rewards_netpol" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata   = { name = "verifier-isolation", namespace = "rewards" }
    spec = {
      podSelector = {}
      policyTypes = ["Ingress", "Egress"]
      egress      = []
      ingress = [{
        from  = [for ns in ["post-training", "platform"] : { namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = ns } } }]
        ports = [{ protocol = "TCP", port = 8080 }]
      }]
    }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the health monitor (Design 06, "Stability: the signals that predict a collapse") --------------------------------
resource "kubernetes_deployment_v1" "monitor" {
  metadata {
    name      = "health-monitor"
    namespace = "platform"
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "health-monitor" } }
    template {
      metadata {
        labels      = { app = "health-monitor" }
        annotations = { "ds06/code-sha" = sha256(file("${path.module}/../../workloads/monitor.py")) }
      }
      spec {
        service_account_name = "health-monitor"
        node_selector        = { "ds06/pool" = "system" }
        container {
          name    = "monitor"
          image   = var.python_image
          command = ["bash", "-c", "pip install -q --no-cache-dir boto3==1.44.9 && exec python -u /code/monitor.py"]
          env_from {
            config_map_ref { name = "platform-env" }
          }
          resources {
            requests = { cpu = "100m", memory = "256Mi" }
          }
          volume_mount {
            name       = "code"
            mount_path = "/code"
          }
        }
        volume {
          name = "code"
          config_map { name = "workload-code" }
        }
      }
    }
  }
  depends_on = [kubernetes_config_map_v1.code, kubernetes_config_map_v1.env, aws_eks_pod_identity_association.identity]
}
