# What the experiments need on the cluster, and nothing that belongs to one experiment: the GPU device plugin, Kueue (the research queue that admits
# the sweep's jobs), and the namespace with its two service accounts. The experiments themselves are applied by the tests, so every one starts from a
# known state.
resource "kubectl_manifest" "namespace" {
  yaml_body = yamlencode({ apiVersion = "v1", kind = "Namespace", metadata = { name = "rl-research" } })
}
resource "kubectl_manifest" "service_accounts" {
  for_each = toset(["trainer", "tools"])
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata   = { name = each.value, namespace = "rl-research" }
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
data "http" "kueue" {
  url = "https://github.com/kubernetes-sigs/kueue/releases/download/${var.versions.kueue}/manifests.yaml"
}
data "kubectl_file_documents" "kueue" { content = data.http.kueue.response_body }
resource "kubectl_manifest" "kueue" {
  for_each          = data.kubectl_file_documents.kueue.manifests
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
}
