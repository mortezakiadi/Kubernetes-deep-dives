# Design 08, "The infrastructure": branch rollouts are admitted by Kueue at a lower priority than primary rollouts,
# so they fill spare capacity and give it back. Both queues are CPU-only (the environments); the rollout engine's
# GPU is a Deployment, outside the queues.
locals {
  env_cpu = 6 # 12 environment pods of 500m
  env_mem = 12
}

resource "kubectl_manifest" "flavor" {
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata   = { name = "env-cpu" }
    spec       = { nodeLabels = { "ds08/pool" = "system" } }
  })
  depends_on = [time_sleep.kueue_ready]
}

resource "kubectl_manifest" "priority" {
  for_each = { primary = 1000, "branch-low" = 100 }
  yaml_body = yamlencode({
    apiVersion  = "kueue.x-k8s.io/v1beta2"
    kind        = "WorkloadPriorityClass"
    metadata    = { name = each.key }
    value       = each.value
    description = "Priority for ${each.key} environment jobs."
  })
  depends_on = [time_sleep.kueue_ready]
}

locals {
  cqs = {
    rollouts = { cpu = local.env_cpu, mem = local.env_mem, borrow = 0, reclaim = "Any" }
    branches = { cpu = 0, mem = 0, borrow = 1, reclaim = "Never" }
  }
}

resource "kubectl_manifest" "cluster_queue" {
  for_each = local.cqs
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata   = { name = each.key }
    spec = {
      cohortName        = "rl-envs"
      namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "rl-envs" } }
      queueingStrategy  = "BestEffortFIFO"
      preemption        = { reclaimWithinCohort = each.value.reclaim, withinClusterQueue = "LowerPriority" }
      resourceGroups = [{
        coveredResources = ["cpu", "memory"]
        flavors = [{
          name = "env-cpu"
          resources = [
            { name = "cpu", nominalQuota = each.value.cpu, borrowingLimit = each.value.borrow * local.env_cpu },
            { name = "memory", nominalQuota = "${each.value.mem}Gi", borrowingLimit = "${each.value.borrow * local.env_mem}Gi" },
          ]
        }]
      }]
    }
  })
  depends_on = [kubectl_manifest.flavor, kubernetes_namespace_v1.ns]
}

resource "kubectl_manifest" "local_queue" {
  for_each = { "rl-rollouts" = "rollouts", "rl-branches" = "branches" }
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata   = { name = each.key, namespace = "rl-envs" }
    spec       = { clusterQueue = each.value }
  })
  depends_on = [kubectl_manifest.cluster_queue]
}
