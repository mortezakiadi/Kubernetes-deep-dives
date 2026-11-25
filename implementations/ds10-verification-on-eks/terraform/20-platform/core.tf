# ---- namespaces ----------------------------------------------------------------------------------------------------
# rewards: every verifier Deployment and the fuzzing jobs (restricted Pod Security). judges: the two judge models.
# training: the red-team RL runs. platform: the store and the review service.
resource "kubernetes_namespace_v1" "ns" {
  for_each = { "rewards" = "restricted", "judges" = "baseline", "training" = "baseline", "platform" = "baseline" }
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
    nodeSelector = { "ds10/pool" = "gpu" }
    affinity     = null
    tolerations  = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
  })]
}

# gVisor: the handler name matches the runtime the sandbox nodes' containerd was given (cluster layer). The scheduling
# block adds the node selector and toleration to every pod that names this class.
resource "kubectl_manifest" "gvisor" {
  yaml_body = yamlencode({
    apiVersion = "node.k8s.io/v1"
    kind       = "RuntimeClass"
    metadata   = { name = "gvisor" }
    handler    = "runsc"
    scheduling = {
      nodeSelector = { "ds10/runtime" = "gvisor" }
      tolerations  = [{ key = "ds10/sandbox", operator = "Equal", value = "gvisor", effect = "NoSchedule" }]
    }
  })
}
