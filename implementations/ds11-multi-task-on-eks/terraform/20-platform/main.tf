# What the experiments need on the cluster, and nothing that belongs to one experiment: the GPU device plugin, Kueue (the research queue that admits
# the sweep's jobs), Argo Workflows (the pipeline engine of S10), and the namespace with its two service accounts. The experiments themselves are applied by the tests, so every one starts from a
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

# Argo Workflows chains the steps of the combination pipeline (train specialists, merge, evaluate, gate). Its steps are Kueue-managed Jobs, so the
# pipeline waits in the same queue as everything else. The runner service account may create and watch Jobs in the experiments' namespace and report
# each step's result to the controller, and nothing more.
data "http" "argo" {
  url = "https://github.com/argoproj/argo-workflows/releases/download/${var.versions.argo}/install.yaml"
}
data "kubectl_file_documents" "argo" { content = data.http.argo.response_body }
# the release manifest assumes the namespace exists
resource "kubectl_manifest" "argo_namespace" {
  yaml_body = yamlencode({ apiVersion = "v1", kind = "Namespace", metadata = { name = "argo" } })
}
resource "kubectl_manifest" "argo" {
  for_each          = data.kubectl_file_documents.argo.manifests
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  depends_on        = [kubectl_manifest.argo_namespace]
}
resource "kubectl_manifest" "argo_runner" {
  for_each = {
    sa = { apiVersion = "v1", kind = "ServiceAccount", metadata = { name = "argo-runner", namespace = "rl-research" } }
    role = {
      apiVersion = "rbac.authorization.k8s.io/v1"
      kind       = "Role"
      metadata   = { name = "argo-runner", namespace = "rl-research" }
      rules = [
        { apiGroups = ["argoproj.io"], resources = ["workflowtaskresults"], verbs = ["create", "patch"] },
        { apiGroups = ["batch"], resources = ["jobs"], verbs = ["create", "get", "list", "watch", "delete"] },
      ]
    }
    binding = {
      apiVersion = "rbac.authorization.k8s.io/v1"
      kind       = "RoleBinding"
      metadata   = { name = "argo-runner", namespace = "rl-research" }
      subjects   = [{ kind = "ServiceAccount", name = "argo-runner", namespace = "rl-research" }]
      roleRef    = { apiGroup = "rbac.authorization.k8s.io", kind = "Role", name = "argo-runner" }
    }
  }
  yaml_body  = yamlencode(each.value)
  depends_on = [kubectl_manifest.namespace]
}
