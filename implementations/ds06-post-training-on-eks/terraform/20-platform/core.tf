# ---- namespaces ----------------------------------------------------------------------------------------------------
# post-training: every run (SFT, RL, distillation, filtering, evaluation). rewards: the verifier pool, which executes
# model-written code, under the "restricted" Pod Security level. platform: the health monitor.
resource "kubernetes_namespace_v1" "ns" {
  for_each = {
    "post-training" = "baseline"
    "rewards"       = "restricted"
    "platform"      = "baseline"
  }
  metadata {
    name = each.key
    labels = {
      "pod-security.kubernetes.io/enforce" = each.value
      "pod-security.kubernetes.io/warn"    = each.value
    }
  }
}

# ---- GPUs: the NVIDIA device plugin, on the GPU pool only ------------------------------------------------------------
# The accelerated AMI brings the driver and the container toolkit; the device plugin advertises nvidia.com/gpu.
resource "helm_release" "device_plugin" {
  name             = "nvidia-device-plugin"
  namespace        = "nvidia-device-plugin"
  create_namespace = true
  repository       = "https://nvidia.github.io/k8s-device-plugin"
  chart            = "nvidia-device-plugin"
  version          = var.versions.device_plugin
  wait             = true
  timeout          = 600
  values = [yamlencode({
    nodeSelector = { "ds06/pool" = "gpu" }
    affinity     = null # the chart's default affinity needs Node Feature Discovery labels, which this build does not run
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
  })]
}

# ---- workload API: JobSet (an RL job is several pod groups that must start and end together) -------------------------
resource "helm_release" "jobset" {
  name             = "jobset"
  namespace        = "jobset-system"
  create_namespace = true
  repository       = "oci://registry.k8s.io/jobset/charts"
  chart            = "jobset"
  version          = var.versions.jobset
  wait             = true
  timeout          = 600
}

# ---- admission: Kueue, installed after JobSet so it finds the CRD it integrates with ---------------------------------
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
    controller:
      groupKindConcurrency:
        Job.batch: 5
        Pod: 5
        Workload.kueue.x-k8s.io: 5
        LocalQueue.kueue.x-k8s.io: 1
        Cohort.kueue.x-k8s.io: 1
        ClusterQueue.kueue.x-k8s.io: 1
        ResourceFlavor.kueue.x-k8s.io: 1
    clientConnection:
      qps: 50
      burst: 100
    # An admitted run whose pods are not all running within the timeout is evicted and requeued as a whole.
    waitForPodsReady:
      timeout: 20m
      blockAdmission: false
      requeuingStrategy:
        timestamp: Eviction
        backoffLimitCount: 2
        backoffBaseSeconds: 60
    integrations:
      frameworks:
      - "batch/job"
      - "jobset.x-k8s.io/jobset"
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
    controllerManager = { replicas = 1 }
  })]
  depends_on = [helm_release.jobset]
}

resource "time_sleep" "kueue_ready" {
  create_duration = "30s"
  depends_on      = [helm_release.kueue]
}
