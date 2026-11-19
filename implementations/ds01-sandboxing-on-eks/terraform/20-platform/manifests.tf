# Applies everything under k8s/ plus the pinned agent-sandbox controller.
#
# Why the manifests are split and templated here instead of using a path-documents data source:
# some values (the sandboxd token digest) are only known after apply, and a for_each key must be
# known at plan time. Splitting the raw files first keeps the keys static; templatestring() then
# fills in the values per document.

data "aws_security_group" "sandbox_nodes" {
  name = "${var.project}-sandbox-nodes"
}

data "http" "controller" {
  url = "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${var.controller_version}/sandbox-with-extensions.yaml"

  lifecycle {
    postcondition {
      condition     = sha256(self.response_body) == var.controller_manifest_sha256
      error_message = "The agent-sandbox release manifest does not match the pinned SHA-256."
    }
  }
}

locals {
  k8s_dir = "${path.module}/../../k8s"

  template_vars = {
    image                 = local.image
    node_role             = "${var.project}-sandbox-node"
    sandbox_sg            = data.aws_security_group.sandbox_nodes.id
    gateway_ip            = var.gateway_ip
    sandboxd_sha256       = sha256(random_password.sandboxd_token.result)
    python_pool_replicas  = var.python_pool_replicas
    compute_pool_replicas = var.compute_pool_replicas
  }

  # file name -> list of YAML documents (comments-only chunks are dropped)
  split_docs = {
    for f in sort(tolist(fileset(local.k8s_dir, "*.yaml"))) :
    f => [for d in split("\n---\n", file("${local.k8s_dir}/${f}")) : d if length(regexall("(?m)^kind:", d)) > 0]
  }

  all_docs = merge([
    for f, docs in local.split_docs : { for i, d in docs : "${f}#${i}" => d }
  ]...)

  bootstrap_keys = [for k in keys(local.all_docs) : k if startswith(k, "00-")]
  workload_keys  = [for k in keys(local.all_docs) : k if !startswith(k, "00-")]

  controller_docs = {
    for i, d in [for d in split("\n---\n", data.http.controller.response_body) : d if length(regexall("(?m)^kind:", d)) > 0] :
    tostring(i) => d
  }
}

# --- namespaces and the controller (CRDs) come first ------------------------------------------
resource "kubectl_manifest" "namespaces" {
  for_each          = { for k in local.bootstrap_keys : k => local.all_docs[k] }
  yaml_body         = templatestring(each.value, local.template_vars)
  server_side_apply = true
}

resource "time_sleep" "namespaces" {
  create_duration = "10s"
  depends_on      = [kubectl_manifest.namespaces]
}

resource "kubectl_manifest" "controller" {
  for_each          = local.controller_docs
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  wait_for_rollout  = true
  depends_on        = [time_sleep.namespaces]
}

# Turns on NetworkPolicy enforcement (EKS Auto Mode ships with it off).
resource "kubernetes_config_map_v1" "network_policy" {
  metadata {
    name      = "amazon-vpc-cni"
    namespace = "kube-system"
  }
  data = { "enable-network-policy-controller" = "true" }
}

resource "time_sleep" "crds" {
  create_duration = "20s"
  depends_on      = [kubectl_manifest.controller]
}

# --- everything else ---------------------------------------------------------------------------
resource "kubectl_manifest" "workloads" {
  for_each          = { for k in local.workload_keys : k => local.all_docs[k] }
  yaml_body         = templatestring(each.value, local.template_vars)
  server_side_apply = true
  force_conflicts   = true

  depends_on = [
    time_sleep.crds,
    terraform_data.build_image,
    kubernetes_secret_v1.egress_secrets,
    kubernetes_secret_v1.platform_secrets,
    kubernetes_secret_v1.router_secrets,
    kubernetes_secret_v1.orchestrator_secrets,
    kubernetes_secret_v1.gateway_ca,
    kubernetes_secret_v1.metrics_api_tls,
    kubernetes_config_map_v1.internal_ca,
    kubernetes_config_map_v1.gateway_ca_cert,
    kubernetes_config_map_v1.network_policy,
    aws_ecr_repository.platform,
  ]
}
