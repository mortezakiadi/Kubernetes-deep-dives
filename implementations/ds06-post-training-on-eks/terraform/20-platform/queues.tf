# Design 06, "The experiment platform on Kubernetes": production runs have reserved capacity and are never preempted;
# small ablations borrow idle GPUs at low priority and give them back when production needs them.
#
# Both queues are in one cohort. "production" owns every GPU in the pool. "ablation" owns none: it can only borrow
# what production is not using, and production reclaims it by preempting the borrower.
locals {
  per_gpu = { cpu = 6, mem = 22 } # what one GPU pod requests (see the launcher); a GPU node has 8 vCPU and 32 GiB
  gpus    = 2
}

resource "kubectl_manifest" "flavor" {
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata   = { name = "gpu" }
    spec = {
      nodeLabels  = { "ds06/pool" = "gpu" }
      tolerations = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    }
  })
  depends_on = [time_sleep.kueue_ready]
}

resource "kubectl_manifest" "priority" {
  for_each = { production = 1000, ablation = 100 }
  yaml_body = yamlencode({
    apiVersion  = "kueue.x-k8s.io/v1beta2"
    kind        = "WorkloadPriorityClass"
    metadata    = { name = each.key }
    value       = each.value
    description = "Priority for ${each.key} runs."
  })
  depends_on = [time_sleep.kueue_ready]
}

locals {
  cqs = {
    production = { gpu = local.gpus, borrow = 0, reclaim = "Any" }
    ablation   = { gpu = 0, borrow = local.gpus, reclaim = "Never" }
  }
}

resource "kubectl_manifest" "cluster_queue" {
  for_each = local.cqs
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata   = { name = each.key }
    spec = {
      cohortName        = "post-training"
      namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "post-training" } }
      queueingStrategy  = "BestEffortFIFO"
      preemption        = { reclaimWithinCohort = each.value.reclaim, withinClusterQueue = "LowerPriority" }
      resourceGroups = [{
        coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
        flavors = [{
          name = "gpu"
          resources = [
            { name = "cpu", nominalQuota = each.value.gpu * local.per_gpu.cpu, borrowingLimit = each.value.borrow * local.per_gpu.cpu },
            { name = "memory", nominalQuota = "${each.value.gpu * local.per_gpu.mem}Gi", borrowingLimit = "${each.value.borrow * local.per_gpu.mem}Gi" },
            { name = "nvidia.com/gpu", nominalQuota = each.value.gpu, borrowingLimit = each.value.borrow },
          ]
        }]
      }]
    }
  })
  depends_on = [kubectl_manifest.flavor, kubernetes_namespace_v1.ns]
}

resource "kubectl_manifest" "local_queue" {
  for_each = local.cqs
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata   = { name = each.key, namespace = "post-training" }
    spec       = { clusterQueue = each.key }
  })
  depends_on = [kubectl_manifest.cluster_queue]
}
