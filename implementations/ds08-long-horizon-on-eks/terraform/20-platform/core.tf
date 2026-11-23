# ---- namespaces ----------------------------------------------------------------------------------------------------
# rl-envs: every environment (primary and branch rollouts), restricted Pod Security. llm: the rollout engine.
# platform: the trajectory store, the verifier, the credit worker and the trainer jobs.
resource "kubernetes_namespace_v1" "ns" {
  for_each = { "rl-envs" = "restricted", "llm" = "baseline", "platform" = "baseline" }
  metadata {
    name = each.key
    labels = {
      "pod-security.kubernetes.io/enforce" = each.value
      "pod-security.kubernetes.io/warn"    = each.value
    }
  }
}

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
    nodeSelector = { "ds08/pool" = "gpu" }
    affinity     = null
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
  })]
}

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
    integrations:
      frameworks:
      - "batch/job"
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
}

resource "time_sleep" "kueue_ready" {
  create_duration = "30s"
  depends_on      = [helm_release.kueue]
}

# ---- snapshot-capable storage for environment volumes (Design 08, "Branching rollouts") ----------------------------
resource "kubernetes_storage_class_v1" "gp3" {
  metadata { name = "env-gp3" }
  storage_provisioner    = "ebs.csi.aws.com"
  volume_binding_mode    = "WaitForFirstConsumer"
  reclaim_policy         = "Delete"
  allow_volume_expansion = true
  parameters             = { type = "gp3", encrypted = "true" }
}

resource "kubectl_manifest" "snapshot_class" {
  yaml_body = yamlencode({
    apiVersion     = "snapshot.storage.k8s.io/v1"
    kind           = "VolumeSnapshotClass"
    metadata       = { name = "env-ebs" }
    driver         = "ebs.csi.aws.com"
    deletionPolicy = "Delete"
  })
}
