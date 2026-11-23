locals {
  wl       = "${path.module}/../../workloads"
  env_code = { for f in ["rollout.py", "agent.py", "sandbox.py", "common.py", "csi_env.py"] : f => file("${local.wl}/${f}") }
  all_code = { for f in fileset(local.wl, "*.py") : f => file("${local.wl}/${f}") }
  bank = merge(
    { for id, _ in local.task_index : "public-${id}.json" => file("${var.tasks_dir}/public/${id}.json") },
    { for id, _ in local.task_index : "hidden-${id}.json" => file("${var.tasks_dir}/hidden/${id}.json") },
  )
}

resource "kubernetes_config_map_v1" "env_code" {
  metadata {
    name      = "env-code"
    namespace = "rl-envs"
  }
  data       = local.env_code
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_config_map_v1" "platform_code" {
  metadata {
    name      = "platform-code"
    namespace = "platform"
  }
  data       = local.all_code
  depends_on = [kubernetes_namespace_v1.ns]
}

# The whole task bank, hidden tests included, readable only by the verifier's pods.
resource "kubernetes_config_map_v1" "bank" {
  metadata {
    name      = "task-bank"
    namespace = "platform"
  }
  data       = local.bank
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the rollout engine: vLLM serving the policy, with prefix caching (branches share their prefix) -------------------
resource "kubernetes_deployment_v1" "vllm" {
  metadata {
    name      = "vllm"
    namespace = "llm"
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "vllm" } }
    template {
      metadata { labels = { app = "vllm" } }
      spec {
        # A Service named "vllm" would inject VLLM_PORT=tcp://..., which vLLM reads as its own setting and rejects.
        enable_service_links = false
        node_selector        = { "ds08/pool" = "gpu" }
        toleration {
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }
        container {
          name  = "vllm"
          image = var.vllm_image
          args = [var.policy.repo, "--revision", var.policy.revision, "--served-model-name", "policy", "--enable-prefix-caching",
          "--max-model-len", "8192", "--gpu-memory-utilization", "0.90", "--seed", "0"]
          env {
            name  = "HF_HOME"
            value = "/cache"
          }
          port { container_port = 8000 }
          resources {
            requests = { cpu = "3", memory = "10Gi", "nvidia.com/gpu" = "1" }
            limits   = { memory = "14Gi", "nvidia.com/gpu" = "1" }
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            period_seconds = 10
          }
          volume_mount {
            name       = "cache"
            mount_path = "/cache"
          }
          volume_mount {
            name       = "shm"
            mount_path = "/dev/shm"
          }
        }
        volume {
          name = "cache"
          empty_dir {}
        }
        volume {
          name = "shm"
          empty_dir {
            medium     = "Memory"
            size_limit = "4Gi"
          }
        }
      }
    }
  }
  wait_for_rollout = false
  depends_on       = [helm_release.device_plugin]
}

resource "kubernetes_service_v1" "vllm" {
  metadata {
    name      = "vllm"
    namespace = "llm"
  }
  spec {
    selector = { app = "vllm" }
    port {
      port        = 8000
      target_port = 8000
    }
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the trajectory store and the verifier --------------------------------------------------------------------------
locals {
  services = {
    trajstore = { cmd = "pip install -q --no-cache-dir boto3==1.44.9 && exec python -u /code/trajstore.py", sa = "trajstore", bank = false }
    verifier  = { cmd = "exec python -u /code/verifier.py", sa = null, bank = true }
  }
}

resource "kubernetes_deployment_v1" "svc" {
  for_each = local.services
  metadata {
    name      = each.key
    namespace = "platform"
  }
  spec {
    replicas = 2
    selector { match_labels = { app = each.key } }
    template {
      metadata {
        labels      = { app = each.key }
        annotations = { "ds08/code-sha" = sha256(jsonencode(local.all_code)) }
      }
      spec {
        service_account_name            = each.value.sa
        automount_service_account_token = false
        node_selector                   = { "ds08/pool" = "system" }
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = each.key
          image   = var.python_image
          command = ["bash", "-c", each.value.cmd]
          env {
            name  = "BUCKET"
            value = aws_s3_bucket.data.id
          }
          env {
            name  = "AWS_REGION"
            value = var.region
          }
          env {
            name  = "TASK_BANK"
            value = "/bank"
          }
          env {
            name  = "HOME"
            value = "/tmp"
          }
          port { container_port = 8080 }
          resources {
            requests = { cpu = "500m", memory = "512Mi" }
            limits   = { cpu = "2", memory = "2Gi" }
          }
          security_context {
            run_as_non_root            = true
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
          readiness_probe {
            exec { command = ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"] }
            period_seconds = 10
          }
          volume_mount {
            name       = "code"
            mount_path = "/code"
          }
          dynamic "volume_mount" {
            for_each = each.value.bank ? [1] : []
            content {
              name       = "bank"
              mount_path = "/bank"
            }
          }
        }
        volume {
          name = "code"
          config_map { name = "platform-code" }
        }
        dynamic "volume" {
          for_each = each.value.bank ? [1] : []
          content {
            name = "bank"
            config_map { name = "task-bank" }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_config_map_v1.platform_code, kubernetes_config_map_v1.bank, time_sleep.identity_ready]
}

resource "kubernetes_service_v1" "svc" {
  for_each = local.services
  metadata {
    name      = each.key
    namespace = "platform"
  }
  spec {
    selector = { app = each.key }
    port {
      port        = 8080
      target_port = 8080
    }
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- network: environments reach only the rollout engine, the store, the verifier and DNS ---------------------------
resource "kubectl_manifest" "env_netpol" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata   = { name = "env-egress", namespace = "rl-envs" }
    spec = {
      podSelector = {}
      policyTypes = ["Ingress", "Egress"]
      ingress     = []
      egress = [
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "llm" } }, podSelector = { matchLabels = { app = "vllm" } } }], ports = [{ protocol = "TCP", port = 8000 }] },
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "platform" } }, podSelector = { matchExpressions = [{ key = "app", operator = "In", values = ["trajstore", "verifier"] }] } }], ports = [{ protocol = "TCP", port = 8080 }] },
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }, podSelector = { matchLabels = { "k8s-app" = "kube-dns" } } }], ports = [{ protocol = "UDP", port = 53 }, { protocol = "TCP", port = 53 }] },
      ]
    }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# The verifier runs agent-written code: no egress at all, and only environments and platform jobs may call it.
resource "kubectl_manifest" "verifier_netpol" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata   = { name = "verifier-isolation", namespace = "platform" }
    spec = {
      podSelector = { matchLabels = { app = "verifier" } }
      policyTypes = ["Ingress", "Egress"]
      egress      = []
      ingress = [{
        from  = [for ns in ["rl-envs", "platform"] : { namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = ns } } }]
        ports = [{ protocol = "TCP", port = 8080 }]
      }]
    }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the credit worker's identity: create and read Jobs in rl-envs, nothing else ------------------------------------
resource "kubernetes_service_account_v1" "credit" {
  metadata {
    name      = "credit-worker"
    namespace = "platform"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_role_v1" "credit" {
  metadata {
    name      = "credit-worker"
    namespace = "rl-envs"
  }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "get", "list"]
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_role_binding_v1" "credit" {
  metadata {
    name      = "credit-worker"
    namespace = "rl-envs"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = "credit-worker"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "credit-worker"
    namespace = "platform"
  }
}
