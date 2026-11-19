# ONE table of team shares. Both the hub's queues and every worker's queues are generated from it, so they
# cannot drift (Design 02: "GitOps generates both from one table of team shares").
#
# Scaled-down numbers. Design 02 has 256 reserved GPUs; this build has two CPU workers of 4 vCPUs each and
# a GPU worker that can hold up to 2 GPUs on-demand and 2 on Spot.
locals {
  teams = {
    llm      = { cpu_per_worker = 2, borrow_cpu = 2, lend_cpu = 1, gpu_ondemand = 1, gpu_spot = 1 }
    vision   = { cpu_per_worker = 1, borrow_cpu = 2, lend_cpu = 1, gpu_ondemand = 1, gpu_spot = 0 }
    research = { cpu_per_worker = 1, borrow_cpu = 4, lend_cpu = 1, gpu_ondemand = 0, gpu_spot = 2 }
  }
  workers_cpu = 2 # number of CPU worker clusters

  team_ns = { for t, v in local.teams : t => { cpu_pods = 20 } }

  priority_classes = { production = 1000, standard = 100, opportunistic = 10 }

  # ---- worker ClusterQueues -------------------------------------------------------------------------
  cpu_worker_queues = {
    for fl in ["cpu-ondemand", "cpu-spot"] : fl => {
      for t, v in local.teams : "${t}-cpu" => {
        cohortName        = "cpu-fleet"
        namespaceSelector = { matchLabels = { "ds02/team" = t } }
        queueingStrategy  = "BestEffortFIFO"
        preemption        = { reclaimWithinCohort = "Any", withinClusterQueue = "LowerPriority" }
        resourceGroups = [{
          coveredResources = ["cpu", "memory"]
          flavors = [{
            name = fl
            resources = [
              { name = "cpu", nominalQuota = v.cpu_per_worker, borrowingLimit = v.borrow_cpu, lendingLimit = v.lend_cpu },
              { name = "memory", nominalQuota = "${v.cpu_per_worker * 2}Gi", borrowingLimit = "${v.borrow_cpu * 2}Gi", lendingLimit = "${v.lend_cpu * 2}Gi" },
            ]
          }]
        }]
      }
    }
  }

  gpu_worker_queues = {
    for t, v in local.teams : "${t}-gpu" => {
      cohortName        = "gpu-fleet"
      namespaceSelector = { matchLabels = { "ds02/team" = t } }
      queueingStrategy  = "BestEffortFIFO"
      preemption        = { reclaimWithinCohort = "Any", withinClusterQueue = "LowerPriority" }
      # Use the team's own Spot share before borrowing another team's on-demand GPU.
      flavorFungibility = { whenCanBorrow = "TryNextFlavor", whenCanPreempt = "MayStopSearch" }
      resourceGroups = [{
        coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
        # Flavors in order of preference: on-demand first, then Spot. A team without a flavor cannot use it.
        flavors = [
          for fl in [{ n = "gpu-ondemand", c = v.gpu_ondemand }, { n = "gpu-spot", c = v.gpu_spot }] : {
            name = fl.n
            resources = [
              { name = "cpu", nominalQuota = fl.c * 3 },
              { name = "memory", nominalQuota = "${fl.c * 12}Gi" },
              { name = "nvidia.com/gpu", nominalQuota = fl.c },
            ]
          } if fl.c > 0
        ]
      }]
    }
  }

  # ---- hub ClusterQueues: each team's share of the whole fleet = the sum across workers ---------------
  hub_queues = merge(
    {
      for t, v in local.teams : "${t}-cpu" => {
        cohortName        = "cpu-fleet"
        namespaceSelector = { matchLabels = { "ds02/team" = t } }
        queueingStrategy  = "BestEffortFIFO"
        preemption        = { reclaimWithinCohort = "Any", withinClusterQueue = "LowerPriority" }
        resourceGroups = [{
          coveredResources = ["cpu", "memory"]
          flavors = [{
            name = "fleet-cpu"
            resources = [
              { name = "cpu", nominalQuota = v.cpu_per_worker * local.workers_cpu, borrowingLimit = v.borrow_cpu * local.workers_cpu, lendingLimit = v.lend_cpu * local.workers_cpu },
              { name = "memory", nominalQuota = "${v.cpu_per_worker * 2 * local.workers_cpu}Gi", borrowingLimit = "${v.borrow_cpu * 2 * local.workers_cpu}Gi", lendingLimit = "${v.lend_cpu * 2 * local.workers_cpu}Gi" },
            ]
          }]
        }]
        admissionChecksStrategy = { admissionChecks = [{ name = "dispatch-cpu" }] }
      }
    },
    {
      for t, v in local.teams : "${t}-gpu" => {
        cohortName        = "gpu-fleet"
        namespaceSelector = { matchLabels = { "ds02/team" = t } }
        queueingStrategy  = "BestEffortFIFO"
        preemption        = { reclaimWithinCohort = "Any", withinClusterQueue = "LowerPriority" }
        resourceGroups = [{
          coveredResources = ["cpu", "memory", "nvidia.com/gpu"]
          flavors = [{
            name = "fleet-gpu"
            resources = [
              { name = "cpu", nominalQuota = (v.gpu_ondemand + v.gpu_spot) * 3 },
              { name = "memory", nominalQuota = "${(v.gpu_ondemand + v.gpu_spot) * 12}Gi" },
              { name = "nvidia.com/gpu", nominalQuota = v.gpu_ondemand + v.gpu_spot },
            ]
          }]
        }]
        admissionChecksStrategy = { admissionChecks = [{ name = "dispatch-gpu" }] }
      }
    },
  )

  # ---- LocalQueues: the only thing users reference ---------------------------------------------------
  lq_cpu = { for t, v in local.teams : "${t}/cpu" => "${t}-cpu" }
  lq_gpu = { for t, v in local.teams : "${t}/gpu" => "${t}-gpu" }

  # ---- node pools (Auto Mode, Karpenter) ----------------------------------------------------------------
  cpu_pool = { for fl, cap in { "cpu-ondemand" = "on-demand", "cpu-spot" = "spot" } : fl => {
    template = {
      metadata = { labels = { "ds02/pool" = fl } }
      spec = {
        nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = "default" }
        requirements = [
          { key = "karpenter.sh/capacity-type", operator = "In", values = [cap] },
          { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
          { key = "eks.amazonaws.com/instance-category", operator = "In", values = ["c", "m"] },
          { key = "eks.amazonaws.com/instance-cpu", operator = "In", values = ["2", "4"] },
          { key = "eks.amazonaws.com/instance-generation", operator = "Gt", values = ["5"] },
        ]
      }
    }
    disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = "1m" }
    limits     = { cpu = "16" }
  } }

  gpu_pool = { for fl, cap in { "gpu-ondemand" = "on-demand", "gpu-spot" = "spot" } : fl => {
    template = {
      metadata = { labels = { "ds02/pool" = fl } }
      spec = {
        nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = "default" }
        # Training and inference nodes carry a taint, so only Kueue-admitted pods (whose flavor adds the
        # toleration) can land there. Nothing else tolerates it.
        taints = [{ key = "nvidia.com/gpu", value = "present", effect = "NoSchedule" }]
        requirements = [
          { key = "karpenter.sh/capacity-type", operator = "In", values = [cap] },
          { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
          { key = "eks.amazonaws.com/instance-family", operator = "In", values = ["g6", "g5", "g6e", "g4dn"] },
          { key = "eks.amazonaws.com/instance-size", operator = "In", values = ["xlarge"] },
        ]
      }
    }
    # Idle GPU nodes are removed quickly; nodes with a running training job are never consolidated.
    disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = "2m" }
    limits     = { "nvidia.com/gpu" = "2" }
  } }
}
