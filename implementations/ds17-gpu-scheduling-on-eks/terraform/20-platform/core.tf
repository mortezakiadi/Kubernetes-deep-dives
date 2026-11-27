# ---- namespaces ------------------------------------------------------------------------------------------------------
# One namespace per team or project; Kueue manages jobs only where they carry a queue label. "nokueue" is for S03's
# jobs that bypass admission on purpose. "platform" holds the recorder; "gpu-real" the real-GPU pods of S02.
locals {
  namespaces = {
    "team-llm"        = "llm"
    "team-vision"     = "vision"
    "team-research"   = "research"
    "research-sweeps" = "research"
    "research-paper"  = "research"
    "nokueue"         = "none"
    "gpu-real"        = "measure"
    "platform"        = "platform"
  }
}

resource "kubernetes_namespace_v1" "ns" {
  for_each = local.namespaces
  metadata {
    name   = each.key
    labels = { team = each.value }
  }
}

# ---- real GPUs: device plugin and DCGM exporter ----------------------------------------------------------------------
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
    nodeSelector = { "ds17/pool" = "gpu" }
    affinity     = null
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
  })]
}

resource "helm_release" "dcgm_exporter" {
  name             = "dcgm-exporter"
  namespace        = "gpu-monitoring"
  create_namespace = true
  repository       = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart            = "dcgm-exporter"
  version          = var.versions.dcgm_exporter
  wait             = true
  timeout          = 900
  values = [yamlencode({
    nodeSelector   = { "ds17/pool" = "gpu" }
    tolerations    = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    serviceMonitor = { enabled = false }
  })]
  depends_on = [helm_release.device_plugin]
}

# ---- KWOK: the simulated GPU fleet ------------------------------------------------------------------------------------
# KWOK v0.8.0 (vendored under platform/kwok, image pinned by digest) manages only nodes annotated kwok.x-k8s.io/node=fake.
# up.sh creates 16 such nodes with 8 fake nvidia.com/gpu each; their pods are never run, only moved through the
# stages, and a pod of a Job completes after the delay in its pod-complete.stage.kwok.x-k8s.io/delay annotation.
data "kubectl_file_documents" "kwok" {
  content = file("${path.module}/../../platform/kwok/controller.yaml")
}

resource "kubectl_manifest" "kwok" {
  for_each   = data.kubectl_file_documents.kwok.manifests
  yaml_body  = each.value
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "time_sleep" "kwok_crds" {
  create_duration = "20s"
  depends_on      = [kubectl_manifest.kwok]
}

data "kubectl_file_documents" "kwok_stages" {
  content = join("\n---\n", [for f in sort(fileset("${path.module}/../../platform/kwok", "stage*.yaml")) : file("${path.module}/../../platform/kwok/${f}")])
}

resource "kubectl_manifest" "kwok_stages" {
  for_each   = data.kubectl_file_documents.kwok_stages.manifests
  yaml_body  = each.value
  depends_on = [time_sleep.kwok_crds]
}
