# ---- namespaces: one per workload family, created by the platform ----------------------------------------
locals {
  # "family" is the Design 03 workload family. Kueue manages pods in these namespaces only when they carry a queue label.
  families = {
    research  = { ns = "research", queue = "notebooks" }
    training  = { ns = "llm-train", queue = "training" }
    inference = { ns = "inference", queue = "serving" }
    batch     = { ns = "batch-eval", queue = "batch" }
  }
  system_namespaces = ["platform", "monitoring", "gpu-buffer", "inference-gw"]
}
resource "kubernetes_namespace_v1" "family" {
  for_each = local.families
  metadata {
    name = each.value.ns
    labels = {
      "ds03/family"                        = each.key
      "pod-security.kubernetes.io/enforce" = "baseline"
      "pod-security.kubernetes.io/warn"    = "baseline"
    }
  }
}
resource "kubernetes_namespace_v1" "system" {
  for_each = toset(local.system_namespaces)
  metadata { name = each.key }
}
# ---- storage class for notebook workspaces ----------------------------------------------------------------
resource "kubernetes_storage_class_v1" "ebs" {
  metadata { name = "ebs-auto" }
  storage_provisioner = "ebs.csi.eks.amazonaws.com"
  volume_binding_mode = "WaitForFirstConsumer"
  reclaim_policy      = "Delete"
  parameters          = { type = "gp3", encrypted = "true" }
}
# ---- workload APIs: Kubeflow Trainer v2 (brings JobSet) and LeaderWorkerSet ------------------------------------
resource "helm_release" "trainer" {
  name             = "kubeflow-trainer"
  namespace        = "kubeflow-system"
  create_namespace = true
  repository       = "oci://ghcr.io/kubeflow/charts"
  chart            = "kubeflow-trainer"
  version          = var.versions.trainer
  wait             = true
  timeout          = 600
}
resource "helm_release" "lws" {
  name             = "lws"
  namespace        = "lws-system"
  create_namespace = true
  repository       = "oci://registry.k8s.io/lws/charts"
  chart            = "lws"
  version          = var.versions.lws
  wait             = true
  timeout          = 600
}
# ---- scheduling: Kueue, installed AFTER the APIs it integrates with, so it finds their CRDs ---------------------
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
        Workload.kueue.x-k8s.io: 10
        LocalQueue.kueue.x-k8s.io: 5
        Cohort.kueue.x-k8s.io: 1
        ClusterQueue.kueue.x-k8s.io: 5
        ResourceFlavor.kueue.x-k8s.io: 1
    clientConnection:
      qps: 300
      burst: 500
    # A gang whose pods are not all running within the timeout is evicted and requeued as a whole.
    waitForPodsReady:
      timeout: 20m
      blockAdmission: false
      requeuingStrategy:
        timestamp: Eviction
        backoffLimitCount: 3
        backoffBaseSeconds: 30
    integrations:
      frameworks:
      - "batch/job"
      - "jobset.x-k8s.io/jobset"
      - "trainer.kubeflow.org/trainjob"
      - "pod"
      - "deployment"
      - "statefulset"
      - "leaderworkerset.x-k8s.io/leaderworkerset"
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
  depends_on = [helm_release.trainer, helm_release.lws]
}
resource "time_sleep" "kueue_ready" {
  create_duration = "30s"
  depends_on      = [helm_release.kueue]
}
# ---- metrics and autoscaling ----------------------------------------------------------------------------------
resource "helm_release" "prometheus" {
  name       = "prometheus"
  namespace  = "monitoring"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = var.versions.prometheus
  wait       = true
  timeout    = 600
  values = [yamlencode({
    alertmanager             = { enabled = false }
    prometheus-pushgateway   = { enabled = false }
    prometheus-node-exporter = { enabled = false }
    kube-state-metrics       = { enabled = true }
    server = {
      persistentVolume = { enabled = false }
      retention        = "6h"
      global           = { scrape_interval = "10s", evaluation_interval = "10s" }
      service          = { type = "ClusterIP" }
    }
  })]
  depends_on = [kubernetes_namespace_v1.system]
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
}
