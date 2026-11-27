# ---- the GPU-hour accounting service (read-only) ---------------------------------------------------------------------
locals {
  code     = { for f in fileset("${path.module}/../../workloads", "*.py") : f => file("${path.module}/../../workloads/${f}") }
  code_sha = sha256(jsonencode(local.code))
}

resource "kubernetes_config_map_v1" "code" {
  metadata {
    name      = "code-${substr(local.code_sha, 0, 10)}"
    namespace = "platform"
  }
  data       = local.code
  immutable  = true
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_service_account_v1" "accounting" {
  metadata {
    name      = "accounting"
    namespace = "platform"
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_cluster_role_v1" "accounting" {
  metadata { name = "ds17-accounting" }
  rule {
    api_groups = [""]
    resources  = ["nodes", "pods"]
    verbs      = ["get", "list"]
  }
  rule {
    api_groups = ["kueue.x-k8s.io"]
    resources  = ["workloads", "clusterqueues", "localqueues"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "accounting" {
  metadata { name = "ds17-accounting" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.accounting.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "accounting"
    namespace = "platform"
  }
}

resource "kubernetes_deployment_v1" "accounting" {
  metadata {
    name      = "accounting"
    namespace = "platform"
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "accounting" } }
    template {
      metadata { labels = { app = "accounting" } }
      spec {
        service_account_name = "accounting"
        node_selector        = { "ds17/pool" = "system" }
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = "accounting"
          image   = var.python_image
          command = ["python", "-u", "/code/accounting.py"]
          port { container_port = 8080 }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { memory = "512Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "code"
            mount_path = "/code"
          }
        }
        volume {
          name = "code"
          config_map { name = kubernetes_config_map_v1.code.metadata[0].name }
        }
      }
    }
  }
  depends_on = [kubernetes_cluster_role_binding_v1.accounting, helm_release.dcgm_exporter]
}

resource "kubernetes_service_v1" "accounting" {
  metadata {
    name      = "accounting"
    namespace = "platform"
  }
  spec {
    selector = { app = "accounting" }
    port {
      port        = 8080
      target_port = 8080
    }
  }
  depends_on = [kubernetes_namespace_v1.ns]
}
