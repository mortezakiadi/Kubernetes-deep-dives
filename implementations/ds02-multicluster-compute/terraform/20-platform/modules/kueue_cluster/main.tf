# Everything one fleet cluster gets from the platform: Kueue, the team namespaces and their walls, the queues,
# and the node pools those queues map to.

locals {
  manager_config = <<-YAML
    apiVersion: config.kueue.x-k8s.io/v1beta2
    kind: Configuration
    health:
      healthProbeBindAddress: :8081
    metrics:
      bindAddress: :8443
    webhook:
      port: 9443
    leaderElection:
      leaderElect: true
      resourceName: c1f6bfd2.kueue.x-k8s.io
    controller:
      groupKindConcurrency:
        Job.batch: 5
        Pod: 5
        Workload.kueue.x-k8s.io: 10
        LocalQueue.kueue.x-k8s.io: 5
        Cohort.kueue.x-k8s.io: 1
        ClusterQueue.kueue.x-k8s.io: 5
        ResourceFlavor.kueue.x-k8s.io: 1
    clientConnection:
      qps: 300
      burst: 500
    # If an admitted job's pods are not all running in time, evict and requeue the whole job.
    waitForPodsReady:
      timeout: 10m
      blockAdmission: false
      requeuingStrategy:
        timestamp: Eviction
        backoffLimitCount: 3
        backoffBaseSeconds: 30
    # Side doors closed: in team namespaces, a Job or Pod with no queue label is held, never run.
    manageJobsWithoutQueueName: true
    managedJobsNamespaceSelector:
      matchExpressions:
      - key: ds02/team
        operator: Exists
    integrations:
      frameworks:
      - "batch/job"
      - "pod"
    fairSharing:
      preemptionStrategies: [LessThanOrEqualToFinalShare, LessThanInitialShare]
  YAML
}

resource "helm_release" "kueue" {
  name             = "kueue"
  namespace        = "kueue-system"
  create_namespace = true
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = var.kueue_version
  wait             = true
  timeout          = 600

  values = [yamlencode({
    managerConfig = { controllerManagerConfigYaml = local.manager_config }
    controllerManager = {
      replicas = 1
    }
  })]
}

resource "time_sleep" "kueue_ready" {
  create_duration = "30s"
  depends_on      = [helm_release.kueue]
}

# EKS Auto Mode ships with NetworkPolicy enforcement off.
resource "kubernetes_config_map_v1" "network_policy" {
  metadata {
    name      = "amazon-vpc-cni"
    namespace = "kube-system"
  }
  data = { "enable-network-policy-controller" = "true" }
}

# ---- team namespaces (created by the platform, never by teams) -------------------------------------
resource "kubernetes_namespace_v1" "team" {
  for_each = var.teams
  metadata {
    name = each.key
    labels = {
      "ds02/team"                          = each.key
      "pod-security.kubernetes.io/enforce" = "restricted"
      "pod-security.kubernetes.io/audit"   = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "kubernetes_resource_quota_v1" "team" {
  for_each = var.teams
  metadata {
    name      = "team-limits"
    namespace = kubernetes_namespace_v1.team[each.key].metadata[0].name
  }
  spec {
    hard = { "count/jobs.batch" = "30", "pods" = tostring(each.value.cpu_pods) }
  }
}

# Default deny for traffic INTO a team namespace. Pods may talk within their own namespace only.
resource "kubernetes_network_policy_v1" "team_isolation" {
  for_each = var.teams
  metadata {
    name      = "isolate-team"
    namespace = kubernetes_namespace_v1.team[each.key].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector {}
      }
    }
  }
  depends_on = [kubernetes_config_map_v1.network_policy]
}

# The identity a team's jobs run as. On workers, Pod Identity associates it with the team's own IAM role.
resource "kubernetes_service_account_v1" "team_job" {
  for_each = var.teams
  metadata {
    name      = "team-job"
    namespace = kubernetes_namespace_v1.team[each.key].metadata[0].name
  }
}

# ---- queues ------------------------------------------------------------------------------------------
resource "kubectl_manifest" "priority_class" {
  for_each = var.priority_classes
  yaml_body = yamlencode({
    apiVersion  = "kueue.x-k8s.io/v1beta2"
    kind        = "WorkloadPriorityClass"
    metadata    = { name = each.key }
    value       = each.value
    description = "Priority for ${each.key} work. Separate from quota."
  })
  depends_on = [time_sleep.kueue_ready]
}

resource "kubectl_manifest" "flavor" {
  for_each = var.flavors
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata   = { name = each.key }
    spec       = each.value
  })
  depends_on = [time_sleep.kueue_ready]
}

resource "kubectl_manifest" "cluster_queue" {
  for_each = var.cluster_queues
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata   = { name = each.key }
    spec       = each.value
  })
  depends_on = [kubectl_manifest.flavor, kubernetes_namespace_v1.team]
}

resource "kubectl_manifest" "local_queue" {
  for_each = var.local_queues
  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata   = { name = split("/", each.key)[1], namespace = split("/", each.key)[0] }
    spec       = { clusterQueue = each.value }
  })
  depends_on = [kubectl_manifest.cluster_queue]
}

# ---- node pools: where each flavor's capacity comes from ------------------------------------------------
resource "kubectl_manifest" "node_pool" {
  for_each = var.node_pools
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = each.key }
    spec       = each.value
  })
}

# ---- the hub's access: a narrowly scoped identity, never cluster-admin ----------------------------------
resource "kubernetes_service_account_v1" "multikueue" {
  count = var.create_multikueue_sa ? 1 : 0
  metadata {
    name      = "multikueue-hub"
    namespace = "kueue-system"
  }
  depends_on = [helm_release.kueue]
}

resource "kubernetes_cluster_role_v1" "multikueue" {
  count = var.create_multikueue_sa ? 1 : 0
  metadata { name = "multikueue-hub" }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["create", "delete", "get", "list", "watch"]
  }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs/status"]
    verbs      = ["get"]
  }
  # The pod integration is on, so the hub also watches the workers' pods.
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["create", "delete", "get", "list", "watch"]
  }
  rule {
    api_groups = ["kueue.x-k8s.io"]
    resources  = ["workloads"]
    verbs      = ["create", "delete", "get", "list", "watch", "update", "patch"]
  }
  rule {
    api_groups = ["kueue.x-k8s.io"]
    resources  = ["workloads/status"]
    verbs      = ["get", "patch", "update"]
  }
  rule {
    api_groups = ["kueue.x-k8s.io"]
    resources  = ["clusterqueues", "localqueues"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "multikueue" {
  count = var.create_multikueue_sa ? 1 : 0
  metadata { name = "multikueue-hub" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.multikueue[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.multikueue[0].metadata[0].name
    namespace = "kueue-system"
  }
}

resource "kubernetes_secret_v1" "multikueue_token" {
  count = var.create_multikueue_sa ? 1 : 0
  metadata {
    name        = "multikueue-hub-token"
    namespace   = "kueue-system"
    annotations = { "kubernetes.io/service-account.name" = kubernetes_service_account_v1.multikueue[0].metadata[0].name }
  }
  type                           = "kubernetes.io/service-account-token"
  wait_for_service_account_token = true
}

output "multikueue_token" {
  value     = var.create_multikueue_sa ? kubernetes_secret_v1.multikueue_token[0].data["token"] : null
  sensitive = true
}
