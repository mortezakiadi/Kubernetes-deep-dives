# ---- the four clusters, each given the same platform from the same module -----------------------------
module "hub" {
  source        = "./modules/kueue_cluster"
  providers     = { kubernetes = kubernetes.hub, kubectl = kubectl.hub, helm = helm.hub }
  cluster_name  = local.eks["hub"].name
  role          = "manager"
  kueue_version = var.kueue_version
  teams         = local.team_ns
  flavors = {
    "fleet-cpu" = {}
    "fleet-gpu" = {}
  }
  cluster_queues   = local.hub_queues
  local_queues     = merge(local.lq_cpu, local.lq_gpu)
  priority_classes = local.priority_classes
}

module "cpu_a" {
  source        = "./modules/kueue_cluster"
  providers     = { kubernetes = kubernetes.cpu_a, kubectl = kubectl.cpu_a, helm = helm.cpu_a }
  cluster_name  = local.eks["cpu-a"].name
  role          = "worker"
  kueue_version = var.kueue_version
  teams         = local.team_ns
  flavors       = { "cpu-ondemand" = { nodeLabels = { "ds02/pool" = "cpu-ondemand" } } }
  cluster_queues = local.cpu_worker_queues["cpu-ondemand"]
  local_queues   = local.lq_cpu
  node_pools     = { "cpu-ondemand" = local.cpu_pool["cpu-ondemand"] }
  priority_classes     = local.priority_classes
  create_multikueue_sa = true
}

module "cpu_b" {
  source        = "./modules/kueue_cluster"
  providers     = { kubernetes = kubernetes.cpu_b, kubectl = kubectl.cpu_b, helm = helm.cpu_b }
  cluster_name  = local.eks["cpu-b"].name
  role          = "worker"
  kueue_version = var.kueue_version
  teams         = local.team_ns
  flavors       = { "cpu-spot" = { nodeLabels = { "ds02/pool" = "cpu-spot" } } }
  cluster_queues = local.cpu_worker_queues["cpu-spot"]
  local_queues   = local.lq_cpu
  node_pools     = { "cpu-spot" = local.cpu_pool["cpu-spot"] }
  priority_classes     = local.priority_classes
  create_multikueue_sa = true
}

module "gpu" {
  source        = "./modules/kueue_cluster"
  providers     = { kubernetes = kubernetes.gpu, kubectl = kubectl.gpu, helm = helm.gpu }
  cluster_name  = local.eks["gpu"].name
  role          = "worker"
  kueue_version = var.kueue_version
  teams         = local.team_ns
  flavors = {
    for fl in ["gpu-ondemand", "gpu-spot"] : fl => {
      nodeLabels  = { "ds02/pool" = fl }
      tolerations = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    }
  }
  cluster_queues       = local.gpu_worker_queues
  local_queues         = local.lq_gpu
  node_pools           = local.gpu_pool
  priority_classes     = local.priority_classes
  create_multikueue_sa = true
}

# ---- MultiKueue: the hub learns about its workers ---------------------------------------------------------
locals {
  worker_tokens = {
    "cpu-a" = module.cpu_a.multikueue_token
    "cpu-b" = module.cpu_b.multikueue_token
    "gpu"   = module.gpu.multikueue_token
  }
}

# One kubeconfig Secret per worker, in the hub's Kueue namespace. Each holds the token of that worker's
# narrowly scoped identity, never an administrator credential.
resource "kubernetes_secret_v1" "worker_kubeconfig" {
  provider = kubernetes.hub
  for_each = local.worker_tokens
  metadata {
    name      = "${each.key}-kubeconfig"
    namespace = "kueue-system"
  }
  data = {
    kubeconfig = yamlencode({
      apiVersion        = "v1"
      kind              = "Config"
      "current-context" = each.key
      clusters          = [{ name = each.key, cluster = { server = local.eks[each.key].endpoint, "certificate-authority-data" = local.eks[each.key].ca_b64 } }]
      users             = [{ name = "hub", user = { token = each.value } }]
      contexts          = [{ name = each.key, context = { cluster = each.key, user = "hub" } }]
    })
  }
  depends_on = [module.hub]
}

resource "kubectl_manifest" "multikueue_cluster" {
  provider = kubectl.hub
  for_each = local.worker_tokens
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "MultiKueueCluster"
    metadata   = { name = each.key }
    spec       = { clusterSource = { kubeConfig = { locationType = "Secret", location = "${each.key}-kubeconfig" } } }
  })
  depends_on = [kubernetes_secret_v1.worker_kubeconfig]
}

resource "kubectl_manifest" "multikueue_config" {
  provider = kubectl.hub
  for_each = { "cpu-workers" = ["cpu-a", "cpu-b"], "gpu-workers" = ["gpu"] }
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "MultiKueueConfig"
    metadata   = { name = each.key }
    spec       = { clusters = each.value }
  })
  depends_on = [kubectl_manifest.multikueue_cluster]
}

resource "kubectl_manifest" "admission_check" {
  provider = kubectl.hub
  for_each = { "dispatch-cpu" = "cpu-workers", "dispatch-gpu" = "gpu-workers" }
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "AdmissionCheck"
    metadata   = { name = each.key }
    spec = {
      controllerName = "kueue.x-k8s.io/multikueue"
      parameters     = { apiGroup = "kueue.x-k8s.io", kind = "MultiKueueConfig", name = each.value }
    }
  })
  depends_on = [kubectl_manifest.multikueue_config]
}

# ---- the hub runs no user workloads ------------------------------------------------------------------------
resource "kubectl_manifest" "hub_no_pods" {
  provider = kubectl.hub
  yaml_body = yamlencode({
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata   = { name = "hub-runs-no-user-pods" }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{ apiGroups = [""], apiVersions = ["v1"], operations = ["CREATE"], resources = ["pods"] }]
      }
      validations = [{
        expression = "false"
        message    = "The hub cluster runs no user workloads. Submit a Job to your LocalQueue; MultiKueue runs it in a worker cluster."
      }]
    }
  })
  depends_on = [module.hub]
}

resource "kubectl_manifest" "hub_no_pods_binding" {
  provider = kubectl.hub
  yaml_body = yamlencode({
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata   = { name = "hub-runs-no-user-pods" }
    spec = {
      policyName        = "hub-runs-no-user-pods"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = { matchExpressions = [{ key = "ds02/team", operator = "Exists" }] }
      }
    }
  })
  depends_on = [kubectl_manifest.hub_no_pods]
}
