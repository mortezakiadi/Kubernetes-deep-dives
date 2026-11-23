# What the experiments need on the cluster, and nothing that belongs to one experiment: the GPU device plugin, JobSet (the trainer's restart
# policy), LeaderWorkerSet (the rollout engines), KEDA (the sandbox pool's autoscaler), and the namespace with its two service accounts.
# The workloads themselves (trainer, rollout servers, environment service) are applied by the tests, so every experiment starts from a known state.
resource "kubectl_manifest" "namespace" {
  yaml_body = yamlencode({ apiVersion = "v1", kind = "Namespace", metadata = { name = "rl-scale" } })
}
resource "kubectl_manifest" "service_accounts" {
  for_each = toset(["trainer", "tools"])
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata   = { name = each.value, namespace = "rl-scale" }
  })
  depends_on = [kubectl_manifest.namespace]
}

# The device plugin makes the GPUs visible to the scheduler as nvidia.com/gpu. The EKS NVIDIA node image already has the driver and the
# container runtime hook, so the plugin is the only piece to add. It runs only on the nodes labelled gpu=true.
resource "helm_release" "device_plugin" {
  name       = "nvidia-device-plugin"
  namespace  = "kube-system"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = var.versions.device_plugin
  wait       = true
  timeout    = 600
  values = [yamlencode({
    affinity     = null
    nodeSelector = { gpu = "true" }
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
  })]
}

data "http" "jobset" {
  url = "https://github.com/kubernetes-sigs/jobset/releases/download/${var.versions.jobset}/manifests.yaml"
}
data "kubectl_file_documents" "jobset" { content = data.http.jobset.response_body }
resource "kubectl_manifest" "jobset" {
  for_each          = data.kubectl_file_documents.jobset.manifests
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
}
data "http" "lws" {
  url = "https://github.com/kubernetes-sigs/lws/releases/download/${var.versions.lws}/manifests.yaml"
}
data "kubectl_file_documents" "lws" { content = data.http.lws.response_body }
resource "kubectl_manifest" "lws" {
  for_each          = data.kubectl_file_documents.lws.manifests
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
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
  values = [yamlencode({ nodeSelector = { pool = "system" } })]
}

# The environment tier's queue. A plain Redis: the sandbox pool reads work from a list in it and KEDA scales on the list's length.
resource "kubectl_manifest" "redis" {
  for_each = {
    deploy = {
      apiVersion = "apps/v1"
      kind       = "Deployment"
      metadata   = { name = "redis", namespace = "rl-scale" }
      spec = {
        replicas = 1
        selector = { matchLabels = { app = "redis" } }
        template = {
          metadata = { labels = { app = "redis" } }
          spec = {
            nodeSelector = { pool = "system" }
            containers = [{
              name      = "redis"
              image     = "redis:7-alpine"
              args      = ["--save", "", "--appendonly", "no"]
              ports     = [{ containerPort = 6379 }]
              resources = { requests = { cpu = "250m", memory = "256Mi" } }
            }]
          }
        }
      }
    }
    svc = {
      apiVersion = "v1"
      kind       = "Service"
      metadata   = { name = "redis", namespace = "rl-scale" }
      spec       = { selector = { app = "redis" }, ports = [{ port = 6379, targetPort = 6379 }] }
    }
  }
  yaml_body  = yamlencode(each.value)
  depends_on = [kubectl_manifest.namespace]
}

# The trainer's start-up guard may delete the rollout group's leader pod, and nothing else: a trainer that died in the middle of a weight push leaves
# the engines blocked inside the NCCL receive, and recreating the group is the only way to free them.
resource "kubectl_manifest" "guard_role" {
  yaml_body = yamlencode({
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "Role"
    metadata   = { name = "fleet-guard", namespace = "rl-scale" }
    rules      = [{ apiGroups = [""], resources = ["pods"], resourceNames = ["rollout-0"], verbs = ["delete"] }]
  })
  depends_on = [kubectl_manifest.namespace]
}
resource "kubectl_manifest" "guard_binding" {
  yaml_body = yamlencode({
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "RoleBinding"
    metadata   = { name = "fleet-guard", namespace = "rl-scale" }
    subjects   = [{ kind = "ServiceAccount", name = "trainer", namespace = "rl-scale" }]
    roleRef    = { apiGroup = "rbac.authorization.k8s.io", kind = "Role", name = "fleet-guard" }
  })
  depends_on = [kubectl_manifest.guard_role, kubectl_manifest.service_accounts]
}
