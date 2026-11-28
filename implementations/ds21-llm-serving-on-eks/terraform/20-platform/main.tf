# What the experiments need on the cluster, and nothing that belongs to one experiment: the GPU device plugin, KEDA (the autoscaler of S10) and a small
# Prometheus (the metrics the autoscaler and the benchmarks read), and the namespace with its service accounts. The servers themselves are applied by the tests.
resource "kubectl_manifest" "namespace" {
  yaml_body = yamlencode({ apiVersion = "v1", kind = "Namespace", metadata = { name = "serving" } })
}
resource "kubectl_manifest" "service_accounts" {
  for_each = toset(["trainer", "tools"])
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata   = { name = each.value, namespace = "serving" }
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
resource "helm_release" "keda" {
  name             = "keda"
  namespace        = "keda"
  create_namespace = true
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.versions.keda
  wait             = true
  timeout          = 600
  values           = [yamlencode({ nodeSelector = { pool = "system" } })]
}
# Prometheus scrapes every pod that carries the standard annotations; the servers' own /metrics endpoints are the only source the tests use.
resource "helm_release" "prometheus" {
  name             = "prometheus"
  namespace        = "monitoring"
  create_namespace = true
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "prometheus"
  version          = var.versions.prometheus
  wait             = true
  timeout          = 900
  values = [yamlencode({
    server                     = { nodeSelector = { pool = "system" }, global = { scrape_interval = "5s", scrape_timeout = "4s" }, persistentVolume = { enabled = false }, retention = "12h" }
    alertmanager               = { enabled = false }
    "prometheus-pushgateway"   = { enabled = false }
    "kube-state-metrics"       = { nodeSelector = { pool = "system" } }
    "prometheus-node-exporter" = { enabled = false }
  })]
}

# GPU utilisation as a metric: the signal the design names as the usual (and wrong) one to scale an inference server on.
resource "helm_release" "dcgm" {
  name       = "dcgm-exporter"
  namespace  = "monitoring"
  repository = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart      = "dcgm-exporter"
  version    = var.versions.dcgm
  wait       = true
  timeout    = 900
  values = [yamlencode({
    nodeSelector   = { pool = "serving" }
    tolerations    = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    podAnnotations = { "prometheus.io/scrape" = "true", "prometheus.io/port" = "9400", "prometheus.io/path" = "/metrics" }
    serviceMonitor = { enabled = false }
  })]
  depends_on = [helm_release.prometheus]
}
