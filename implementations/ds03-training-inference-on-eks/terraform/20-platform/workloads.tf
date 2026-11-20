# The code the platform ships to workloads, as ConfigMaps. No custom images are built: the code is mounted into
# public base images, which keeps the whole implementation reproducible from this repository alone.
locals {
  wl = "${path.module}/../../workloads"
}
resource "kubernetes_config_map_v1" "train_code" {
  metadata {
    name      = "workload-code"
    namespace = "llm-train"
  }
  data       = { "train.py" = file("${local.wl}/train.py"), "entry.sh" = file("${local.wl}/entry.sh") }
  depends_on = [kubernetes_namespace_v1.family]
}
resource "kubernetes_config_map_v1" "eval_code" {
  metadata {
    name      = "workload-code"
    namespace = "batch-eval"
  }
  data       = { "eval.py" = file("${local.wl}/eval.py"), "batch_job.py" = file("${local.wl}/batch_job.py") }
  depends_on = [kubernetes_namespace_v1.family]
}
resource "kubernetes_config_map_v1" "platform_code" {
  metadata {
    name      = "workload-code"
    namespace = "platform"
  }
  data = {
    "modelops.py"        = file("${local.wl}/modelops.py")
    "loadgen.py"         = file("${local.wl}/loadgen.py")
    "notebook_culler.py" = file("${local.wl}/notebook_culler.py")
  }
  depends_on = [kubernetes_namespace_v1.system]
}
# ---- Training: the platform's runtime (Design 03: "the platform runtime is where the expertise lives") ----------
resource "kubectl_manifest" "training_runtime" {
  yaml_body = yamlencode({
    apiVersion = "trainer.kubeflow.org/v1alpha1"
    kind       = "ClusterTrainingRuntime"
    metadata   = { name = "torch-gpu-ddp", labels = { "trainer.kubeflow.org/framework" = "torch" } }
    spec = {
      mlPolicy = { numNodes = 1, torch = { numProcPerNode = "1" } }
      template = {
        spec = {
          # The job controller restarts the whole group when one member fails, up to three times.
          failurePolicy = { maxRestarts = 3 }
          replicatedJobs = [{
            name = "node"
            template = {
              metadata = { labels = { "trainer.kubeflow.org/trainjob-ancestor-step" = "trainer" } }
              spec = {
                backoffLimit = 0
                template = {
                  metadata = { annotations = { "prometheus.io/scrape" = "true", "prometheus.io/port" = "9100", "prometheus.io/path" = "/metrics" } }
                  spec = {
                    serviceAccountName = "trainer"
                    restartPolicy      = "Never"
                    # Placement as policy: every pod of one training job lands in the same zone, so the
                    # gradient exchange never crosses a zone boundary.
                    affinity = {
                      podAffinity = {
                        requiredDuringSchedulingIgnoredDuringExecution = [{
                          topologyKey    = "topology.kubernetes.io/zone"
                          labelSelector  = { matchExpressions = [{ key = "jobset.sigs.k8s.io/replicatedjob-name", operator = "In", values = ["node"] }] }
                          matchLabelKeys = ["jobset.sigs.k8s.io/jobset-name"]
                        }]
                      }
                    }
                    containers = [{
                      name    = "node"
                      image   = "pytorch/pytorch:${var.versions.pytorch}"
                      command = ["bash", "/workloads/entry.sh"]
                      env = [
                        { name = "NODE_NAME", valueFrom = { fieldRef = { fieldPath = "spec.nodeName" } } },
                        { name = "NCCL_DEBUG", value = "WARN" },
                        { name = "NCCL_IB_DISABLE", value = "1" },
                        { name = "NCCL_SOCKET_IFNAME", value = "eth0" },
                      ]
                      envFrom      = [{ configMapRef = { name = "platform-env" } }]
                      volumeMounts = [{ name = "code", mountPath = "/workloads" }, { name = "shm", mountPath = "/dev/shm" }]
                    }]
                    volumes = [
                      { name = "code", configMap = { name = "workload-code" } },
                      { name = "shm", emptyDir = { medium = "Memory", sizeLimit = "2Gi" } },
                    ]
                  }
                }
              }
            }
          }]
        }
      }
    }
  })
  depends_on = [helm_release.trainer, kubernetes_config_map_v1.train_code, kubernetes_config_map_v1.platform_env]
}
# ---- Research: the idle culler ------------------------------------------------------------------------------------
resource "kubernetes_service_account_v1" "culler" {
  metadata {
    name      = "culler"
    namespace = "platform"
  }
  depends_on = [kubernetes_namespace_v1.system]
}
resource "kubernetes_role_v1" "culler" {
  metadata {
    name      = "culler"
    namespace = "research"
  }
  rule {
    api_groups = ["apps"]
    resources  = ["statefulsets", "statefulsets/scale"]
    verbs      = ["get", "list", "patch"]
  }
  depends_on = [kubernetes_namespace_v1.family]
}
resource "kubernetes_role_binding_v1" "culler" {
  metadata {
    name      = "culler"
    namespace = "research"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.culler.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "culler"
    namespace = "platform"
  }
}
resource "kubectl_manifest" "culler" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "notebook-culler", namespace = "platform" }
    spec = {
      replicas = 1
      selector = { matchLabels = { app = "notebook-culler" } }
      template = {
        metadata = { labels = { app = "notebook-culler" } }
        spec = {
          serviceAccountName = "culler"
          containers = [{
            name         = "culler"
            image        = "public.ecr.aws/docker/library/python:3.12-slim"
            command      = ["python", "-u", "/code/notebook_culler.py"]
            env          = [{ name = "NAMESPACE", value = "research" }, { name = "IDLE_SECONDS", value = "120" }, { name = "POLL_SECONDS", value = "15" }]
            resources    = { requests = { cpu = "50m", memory = "64Mi" } }
            volumeMounts = [{ name = "code", mountPath = "/code" }]
          }]
          volumes = [{ name = "code", configMap = { name = "workload-code" } }]
        }
      }
    }
  })
  depends_on = [kubernetes_config_map_v1.platform_code, kubernetes_service_account_v1.culler]
}
# ---- GPU burn-in and buffer: priorities for the scheduler (separate from Kueue's) -------------------------------------
resource "kubernetes_priority_class_v1" "serving" {
  metadata { name = "serving-high" }
  value          = 100000
  global_default = false
  description    = "Model servers. May displace the warm-buffer placeholders."
}
resource "kubernetes_priority_class_v1" "buffer" {
  metadata { name = "buffer-low" }
  value          = -100
  global_default = false
  description    = "Placeholder pods that keep a GPU node warm. Evicted the moment a real replica needs the space."
}
