# Design 03, "Scheduling and capacity": one GPU pool per workload family, one Kueue queue per family, all in one
# cohort so idle quota can be borrowed and taken back.
locals {
  gpu_taint = [{ key = "nvidia.com/gpu", value = "present", effect = "NoSchedule" }]
  # Any one-GPU instance from these families. Karpenter picks the cheapest one that has capacity. Capacity for
  # GPU instances is not guaranteed in any zone, so the list is wide.
  pools = {
    research = {
      gpu_limit = 1
      idle      = "2m"
      families  = ["g6", "g5", "g6e", "g4dn"]
      zones     = null
    }
    training = {
      gpu_limit = 2
      idle      = "6m"
      families  = ["g6", "g5", "g6e", "g4dn"]
      zones     = null
    }
    inference = {
      gpu_limit = 3 # two for model replicas, one for the warm buffer
      idle      = "3m"
      families  = ["g6", "g5", "g6e", "g4dn"]
      zones     = null
    }
  }
}
# One NodeClass for the GPU pools. It selects every private subnet tagged for nodes, which includes two zones
# the cluster itself was not created in: GPU capacity is scarce, so more zones means more chances to get a node.
resource "kubectl_manifest" "node_class" {
  yaml_body = yamlencode({
    apiVersion = "eks.amazonaws.com/v1"
    kind       = "NodeClass"
    metadata   = { name = "gpu" }
    spec = {
      role                       = "${var.project}-node"
      subnetSelectorTerms        = [{ tags = { "ds03/nodes" = "true" } }]
      securityGroupSelectorTerms = [{ tags = { "aws:eks:cluster-name" = var.project } }]
      ephemeralStorage           = { size = "120Gi" } # serving and training images are large
    }
  })
}
resource "kubectl_manifest" "node_pool" {
  for_each = local.pools
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "gpu-${each.key}" }
    spec = {
      template = {
        metadata = { labels = { "ds03/pool" = each.key } }
        spec = {
          nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = "gpu" }
          # Only pods that Kueue admitted (their flavor adds this toleration) can land on a GPU node.
          taints = local.gpu_taint
          requirements = concat([
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "eks.amazonaws.com/instance-family", operator = "In", values = each.value.families },
            { key = "eks.amazonaws.com/instance-size", operator = "In", values = ["xlarge", "2xlarge", "4xlarge", "8xlarge"] },
          ], each.value.zones == null ? [] : [{ key = "topology.kubernetes.io/zone", operator = "In", values = each.value.zones }])
        }
      }
      disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = each.value.idle }
      limits     = { "nvidia.com/gpu" = tostring(each.value.gpu_limit) }
    }
  })
  depends_on = [kubectl_manifest.node_class]
}
# ---- priorities: separate from quota ------------------------------------------------------------------------
resource "kubectl_manifest" "priority" {
  for_each = { production = 1000, training = 500, opportunistic = 100 }
  yaml_body = yamlencode({
    apiVersion  = "kueue.x-k8s.io/v1beta2"
    kind        = "WorkloadPriorityClass"
    metadata    = { name = each.key }
    value       = each.value
    description = "Priority for ${each.key} work."
  })
  depends_on = [time_sleep.kueue_ready]
}
resource "kubectl_manifest" "flavor" {
  for_each = local.pools
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata   = { name = "gpu-${each.key}" }
    spec = {
      nodeLabels  = { "ds03/pool" = each.key }
      tolerations = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
    }
  })
  depends_on = [time_sleep.kueue_ready]
}
locals {
  # Each GPU node is one GPU, 4 vCPU, 16 GiB. A GPU workload asks for 3 vCPU and 12 GiB per GPU.
  per_gpu = { cpu = 3, mem = 12 }
  cqs = {
    # Research: a small guaranteed share, lends it all, and can borrow.
    notebooks = {
      ns     = "research"
      flavor = "gpu-research"
      gpu    = 1
    }
    # Training: two GPUs, reclaims anything that borrowed them.
    training = {
      ns     = "llm-train"
      flavor = "gpu-training"
      gpu    = 2
    }
    # Serving: two GPUs of guaranteed capacity; only one of them may be lent out, so there is always a floor.
    serving = {
      ns     = "inference"
      flavor = "gpu-inference"
      gpu    = 2
      lend   = 1
      borrow = 0
    }
  }
}
resource "kubectl_manifest" "cluster_queue" {
  for_each = local.cqs
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata   = { name = each.key }
    spec = {
      cohortName        = "gpu-fleet"
      namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = each.value.ns } }
      queueingStrategy  = "BestEffortFIFO"
      preemption        = { reclaimWithinCohort = "Any", withinClusterQueue = "LowerPriority" }
      flavorFungibility = { whenCanBorrow = "TryNextFlavor", whenCanPreempt = "TryNextFlavor" }
      resourceGroups = [{
        coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
        flavors = [{
          name = each.value.flavor
          resources = [
            for r in [
              { name = "cpu", nominalQuota = each.value.gpu * local.per_gpu.cpu, lendingLimit = try(each.value.lend * local.per_gpu.cpu, null), borrowingLimit = try(each.value.borrow * local.per_gpu.cpu, null) },
              { name = "memory", nominalQuota = "${each.value.gpu * local.per_gpu.mem}Gi", lendingLimit = try("${each.value.lend * local.per_gpu.mem}Gi", null), borrowingLimit = try("${each.value.borrow * local.per_gpu.mem}Gi", null) },
              { name = "nvidia.com/gpu", nominalQuota = each.value.gpu, lendingLimit = try(each.value.lend, null), borrowingLimit = try(each.value.borrow, null) },
            ] : { for k, v in r : k => v if v != null }
          ]
        }]
      }]
    }
  })
  depends_on = [kubectl_manifest.flavor, kubernetes_namespace_v1.family]
}
# Batch inference and evaluation: no GPUs of their own. They run on whatever the serving queue is not using.
resource "kubectl_manifest" "cluster_queue_batch" {
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata   = { name = "batch" }
    spec = {
      cohortName        = "gpu-fleet"
      namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "batch-eval" } }
      queueingStrategy  = "BestEffortFIFO"
      preemption        = { reclaimWithinCohort = "Never", withinClusterQueue = "LowerPriority" }
      resourceGroups = [{
        coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
        flavors = [{
          name = "gpu-inference"
          resources = [
            { name = "cpu", nominalQuota = 0 },
            { name = "memory", nominalQuota = "0Gi" },
            { name = "nvidia.com/gpu", nominalQuota = 0 },
          ]
        }]
      }]
    }
  })
  depends_on = [kubectl_manifest.flavor, kubernetes_namespace_v1.family]
}
resource "kubectl_manifest" "local_queue" {
  for_each = merge(
    { for k, v in local.cqs : k => { ns = v.ns, cq = k } },
    { batch = { ns = "batch-eval", cq = "batch" } },
  )
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata   = { name = each.key, namespace = each.value.ns }
    spec       = { clusterQueue = each.value.cq }
  })
  depends_on = [kubectl_manifest.cluster_queue, kubectl_manifest.cluster_queue_batch]
}
