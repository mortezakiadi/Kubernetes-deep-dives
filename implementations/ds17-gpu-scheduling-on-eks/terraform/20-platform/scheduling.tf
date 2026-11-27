# ---- admission: Kueue 0.20.8 ----------------------------------------------------------------------------------------
# The design's Configuration, with timeouts scaled to a test run: all pods of an admitted workload must be ready within
# 60 s (the design's 10 min covers image pulls of real jobs), and requeue backoff starts at 10 s.
locals {
  kueue_config = <<-YAML
    apiVersion: config.kueue.x-k8s.io/v1beta2
    kind: Configuration
    health:
      healthProbeBindAddress: :8081
    metrics:
      bindAddress: :8443
    webhook:
      port: 9443
    leaderElection:
      leaderElect: true
      resourceName: c1f6bfd2.kueue.x-k8s.io
    clientConnection:
      qps: 100
      burst: 200
    integrations:
      frameworks:
      - "batch/job"
    waitForPodsReady:
      timeout: 60s
      recoveryTimeout: 60s
      blockAdmission: false
      requeuingStrategy:
        timestamp: Eviction
        backoffBaseSeconds: 10
        backoffMaxSeconds: 120
        backoffLimitCount: 5
    fairSharing:
      preemptionStrategies: [LessThanOrEqualToFinalShare, LessThanInitialShare]
    admissionFairSharing:
      usageHalfLifeTime: 10m
      usageSamplingInterval: 10s
  YAML
}

resource "helm_release" "kueue" {
  name             = "kueue"
  namespace        = "kueue-system"
  create_namespace = true
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = var.versions.kueue
  wait             = true
  timeout          = 600
  values = [yamlencode({
    managerConfig     = { controllerManagerConfigYaml = local.kueue_config }
    controllerManager = { replicas = 1, manager = { nodeSelector = { "ds17/pool" = "system" } } }
  })]
}

resource "time_sleep" "kueue_ready" {
  create_duration = "30s"
  depends_on      = [helm_release.kueue]
}

# The fabric of the simulated fleet: block, rack, host. The flavor points at the fake nodes and enables TAS.
resource "kubectl_manifest" "topology" {
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "Topology"
    metadata   = { name = "ds17-fabric" }
    spec       = { levels = [{ nodeLabel = "network.ds17.io/block" }, { nodeLabel = "network.ds17.io/rack" }, { nodeLabel = "kubernetes.io/hostname" }] }
  })
  depends_on = [time_sleep.kueue_ready]
}

# Two flavors over the same nodes. fake-h100 uses the topology, so Kueue chooses the domain (and the nodes) at admission;
# fake-h100-plain does not, so kube-scheduler alone places the pods (S06 compares scheduler profiles with it).
resource "kubectl_manifest" "flavor" {
  for_each = { "fake-h100" = true, "fake-h100-plain" = false }
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata   = { name = each.key }
    spec = merge({
      nodeLabels  = { "ds17/pool" = "fake-gpu" }
      tolerations = [{ key = "kwok.x-k8s.io/node", operator = "Exists", effect = "NoSchedule" }]
    }, each.value ? { topologyName = "ds17-fabric" } : {})
  })
  depends_on = [kubectl_manifest.topology]
}

resource "kubectl_manifest" "priorities" {
  for_each = { release = 1000, standard = 100, "best-effort" = 10 }
  yaml_body = yamlencode({
    apiVersion  = "kueue.x-k8s.io/v1beta2"
    kind        = "WorkloadPriorityClass"
    metadata    = { name = each.key }
    value       = each.value
    description = "Design 17 priority class ${each.key}."
  })
  depends_on = [time_sleep.kueue_ready]
}

# ---- placement: a second kube-scheduler with two GPU profiles ---------------------------------------------------------
# EKS does not expose the default scheduler's configuration, so the design's profiles run as a second scheduler. Both
# profiles score GPUs ten times heavier than CPU and memory; they differ only in the strategy, so S06 compares exactly that.
locals {
  scoring = [{ name = "nvidia.com/gpu", weight = 10 }, { name = "cpu", weight = 1 }, { name = "memory", weight = 1 }]
  scheduler_config = yamlencode({
    apiVersion     = "kubescheduler.config.k8s.io/v1"
    kind           = "KubeSchedulerConfiguration"
    leaderElection = { leaderElect = true, resourceName = "ds17-scheduler", resourceNamespace = "kube-system" }
    profiles = [for name, strategy in { "gpu-binpack" = "MostAllocated", "gpu-spread" = "LeastAllocated" } : {
      schedulerName = name
      pluginConfig  = [{ name = "NodeResourcesFit", args = { scoringStrategy = { type = strategy, resources = local.scoring } } }]
      plugins       = { score = { disabled = [{ name = "NodeResourcesBalancedAllocation" }] } }
    }]
  })
}

resource "kubernetes_service_account_v1" "scheduler" {
  metadata {
    name      = "ds17-scheduler"
    namespace = "kube-system"
  }
}

resource "kubernetes_cluster_role_binding_v1" "scheduler" {
  for_each = toset(["system:kube-scheduler", "system:volume-scheduler"])
  metadata { name = "ds17-scheduler-${replace(each.key, ":", "-")}" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = each.key
  }
  subject {
    kind      = "ServiceAccount"
    name      = "ds17-scheduler"
    namespace = "kube-system"
  }
}

# system:kube-scheduler allows leases only for the name kube-scheduler; this scheduler uses its own lease.
resource "kubernetes_cluster_role_v1" "scheduler_extra" {
  metadata { name = "ds17-scheduler-extra" }
  rule {
    api_groups = ["coordination.k8s.io"]
    resources  = ["leases"]
    verbs      = ["create", "get", "list", "update", "watch"]
  }
  rule {
    api_groups = [""]
    resources  = ["configmaps"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["csidrivers", "csinodes", "csistoragecapacities", "storageclasses", "volumeattachments"]
    verbs      = ["get", "list", "watch"]
  }
  rule {
    api_groups = ["resource.k8s.io"]
    resources  = ["deviceclasses", "resourceclaims", "resourceslices", "resourceclaimtemplates"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "scheduler_extra" {
  metadata { name = "ds17-scheduler-extra" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.scheduler_extra.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "ds17-scheduler"
    namespace = "kube-system"
  }
}

resource "kubernetes_role_binding_v1" "scheduler_auth" {
  metadata {
    name      = "ds17-scheduler-auth-reader"
    namespace = "kube-system"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = "extension-apiserver-authentication-reader"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "ds17-scheduler"
    namespace = "kube-system"
  }
}

resource "kubernetes_config_map_v1" "scheduler" {
  metadata {
    name      = "ds17-scheduler-config"
    namespace = "kube-system"
  }
  data = { "config.yaml" = local.scheduler_config }
}

resource "kubernetes_deployment_v1" "scheduler" {
  metadata {
    name      = "ds17-scheduler"
    namespace = "kube-system"
  }
  spec {
    replicas = 1
    selector { match_labels = { app = "ds17-scheduler" } }
    template {
      metadata {
        labels      = { app = "ds17-scheduler" }
        annotations = { "ds17/config-sha" = sha256(local.scheduler_config) }
      }
      spec {
        service_account_name = "ds17-scheduler"
        node_selector        = { "ds17/pool" = "system" }
        container {
          name    = "scheduler"
          image   = var.scheduler_image
          command = ["kube-scheduler", "--config=/etc/ds17/config.yaml", "--v=2"]
          resources {
            requests = { cpu = "200m", memory = "256Mi" }
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/ds17"
          }
        }
        volume {
          name = "config"
          config_map { name = "ds17-scheduler-config" }
        }
      }
    }
  }
  depends_on = [kubernetes_cluster_role_binding_v1.scheduler, kubernetes_cluster_role_binding_v1.scheduler_extra, kubernetes_role_binding_v1.scheduler_auth]
}
