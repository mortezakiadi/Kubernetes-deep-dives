# What runs on one cluster of Design 05: the load balancer controller, a node-local DNS cache, the test applications, and (where asked) the
# Cilium CNI chained behind the VPC CNI or the VPC Lattice controller.
terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    kubectl = { source = "alekc/kubectl", version = "~> 2.1" }
    helm    = { source = "hashicorp/helm", version = "~> 3.0" }
    http    = { source = "hashicorp/http", version = "~> 3.4" }
  }
}

variable "project" { type = string }
variable "cluster_name" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "alb_name" { type = string }
variable "cilium" {
  type    = bool
  default = false
}
variable "lattice" {
  type    = bool
  default = false
}
variable "lattice_service_network" {
  type    = string
  default = ""
}
variable "versions" {
  type = map(string)
  default = {
    lbc     = "3.6.3"
    cilium  = "1.20.5"
    lattice = "v2.2.0"
  }
}

data "aws_eks_cluster" "this" { name = var.cluster_name }
locals {
  service_cidr = data.aws_eks_cluster.this.kubernetes_network_config[0].service_ipv4_cidr
  kube_dns_ip  = cidrhost(local.service_cidr, 10)
}

# ---- Pod Identity, so controllers get AWS permissions without any stored credential -------------------------------------
resource "aws_eks_addon" "pod_identity" {
  cluster_name = var.cluster_name
  addon_name   = "eks-pod-identity-agent"
}
data "aws_iam_policy_document" "pods_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# ---- AWS Load Balancer Controller ----------------------------------------------------------------------------------------
resource "aws_iam_role" "lbc" {
  name               = "${var.cluster_name}-lbc"
  assume_role_policy = data.aws_iam_policy_document.pods_trust.json
}
resource "aws_iam_role_policy" "lbc" {
  name   = "lbc"
  role   = aws_iam_role.lbc.id
  policy = file("${path.module}/../../iam_policy_lbc.json")
}
resource "aws_eks_pod_identity_association" "lbc" {
  cluster_name    = var.cluster_name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.lbc.arn
  depends_on      = [aws_eks_addon.pod_identity]
}
resource "helm_release" "lbc" {
  name       = "aws-load-balancer-controller"
  namespace  = "kube-system"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.versions.lbc
  wait       = true
  timeout    = 600
  values = [yamlencode({
    clusterName    = var.cluster_name
    region         = var.region
    vpcId          = var.vpc_id
    replicaCount   = 2
    serviceAccount = { create = true, name = "aws-load-balancer-controller" }
  })]
  depends_on = [aws_eks_pod_identity_association.lbc, helm_release.cilium]
}

# ---- NodeLocal DNSCache --------------------------------------------------------------------------------------------------
locals {
  nodelocal = replace(replace(replace(file("${path.module}/nodelocaldns.yaml"),
    "__PILLAR__DNS__DOMAIN__", "cluster.local"),
    "__PILLAR__LOCAL__DNS__", "169.254.20.10"),
  "__PILLAR__DNS__SERVER__", local.kube_dns_ip)
}
data "kubectl_file_documents" "nodelocal" { content = local.nodelocal }
resource "kubectl_manifest" "nodelocal" {
  for_each  = data.kubectl_file_documents.nodelocal.manifests
  yaml_body = each.value
}

# ---- namespaces and test applications -----------------------------------------------------------------------------------
resource "kubectl_manifest" "ns" {
  for_each = toset(["shop", "payments", "dev", "monitoring"])
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Namespace"
    metadata = {
      name = each.key
      labels = merge({ "pod-security.kubernetes.io/enforce" = "baseline" },
        # In shop, a pod is Ready only once the load balancer reports its address healthy (a pod readiness gate).
      each.key == "shop" ? { "elbv2.k8s.aws/pod-readiness-gate-inject" = "enabled" } : {})
    }
  })
}

# A web service of six replicas, spread over the zones: the one the load balancer, DNS and routing tests call.
resource "kubectl_manifest" "web" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "web", namespace = "shop" }
    spec = {
      replicas = 6
      selector = { matchLabels = { app = "web" } }
      template = {
        metadata = { labels = { app = "web" } }
        spec = {
          topologySpreadConstraints = [{
            maxSkew       = 1, topologyKey = "topology.kubernetes.io/zone", whenUnsatisfiable = "DoNotSchedule",
            labelSelector = { matchLabels = { app = "web" } }
          }]
          containers = [{
            name           = "web"
            image          = "traefik/whoami:v1.10.3"
            args           = ["--name", var.cluster_name]
            ports          = [{ containerPort = 80 }]
            readinessProbe = { httpGet = { path = "/health", port = 80 }, periodSeconds = 3 }
            resources      = { requests = { cpu = "20m", memory = "16Mi" } }
          }]
        }
      }
    }
  })
  depends_on = [kubectl_manifest.ns, helm_release.cilium, helm_release.lbc]
}
resource "kubectl_manifest" "web_svc" {
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Service"
    metadata   = { name = "web", namespace = "shop" }
    spec       = { selector = { app = "web" }, ports = [{ port = 80, targetPort = 80 }] }
  })
  depends_on = [kubectl_manifest.ns, helm_release.lbc]
}
resource "kubectl_manifest" "web_ingress" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "Ingress"
    metadata = {
      name      = "web"
      namespace = "shop"
      annotations = {
        "alb.ingress.kubernetes.io/load-balancer-name"           = var.alb_name
        "alb.ingress.kubernetes.io/scheme"                       = "internal"
        "alb.ingress.kubernetes.io/target-type"                  = "ip"
        "alb.ingress.kubernetes.io/inbound-cidrs"                = "10.60.0.0/14"
        "alb.ingress.kubernetes.io/healthcheck-path"             = "/health"
        "alb.ingress.kubernetes.io/healthcheck-interval-seconds" = "5"
        "alb.ingress.kubernetes.io/healthcheck-timeout-seconds"  = "2"
        "alb.ingress.kubernetes.io/healthy-threshold-count"      = "2"
        "alb.ingress.kubernetes.io/unhealthy-threshold-count"    = "2"
        "alb.ingress.kubernetes.io/target-group-attributes"      = "deregistration_delay.timeout_seconds=30"
      }
    }
    spec = {
      ingressClassName = "alb"
      rules            = [{ http = { paths = [{ path = "/", pathType = "Prefix", backend = { service = { name = "web", port = { number = 80 } } } }] } }]
    }
  })
  depends_on = [helm_release.lbc, kubectl_manifest.web_svc]
}

# The sensitive service the isolation tests protect.
resource "kubectl_manifest" "ledger" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "ledger", namespace = "payments" }
    spec = {
      replicas = 2
      selector = { matchLabels = { app = "ledger" } }
      template = {
        metadata = { labels = { app = "ledger" } }
        spec = {
          containers = [{ name = "ledger", image = "traefik/whoami:v1.10.3", args = ["--name", "ledger"], ports = [{ containerPort = 80 }], resources = { requests = { cpu = "10m", memory = "16Mi" } } }]
        }
      }
    }
  })
  depends_on = [kubectl_manifest.ns, helm_release.cilium, helm_release.lbc]
}
resource "kubectl_manifest" "ledger_svc" {
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Service"
    metadata   = { name = "ledger", namespace = "payments" }
    spec       = { selector = { app = "ledger" }, ports = [{ port = 80, targetPort = 80 }] }
  })
  depends_on = [kubectl_manifest.ns, helm_release.lbc]
}

# A shell with the usual network tools, in several namespaces, for the tests to run commands from.
resource "kubectl_manifest" "probe" {
  for_each = toset(["shop", "dev", "payments", "monitoring"])
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "probe", namespace = each.key }
    spec = {
      replicas = 1
      selector = { matchLabels = { app = "probe" } }
      template = {
        metadata = { labels = { app = "probe" } }
        spec = {
          containers = [{ name = "probe", image = "nicolaka/netshoot:v0.13", command = ["sleep", "infinity"], resources = { requests = { cpu = "10m", memory = "32Mi" } } }]
        }
      }
    }
  })
  depends_on = [kubectl_manifest.ns, helm_release.cilium, helm_release.lbc]
}

# ---- Cilium, chained behind the VPC CNI (only where asked) ------------------------------------------------------------------
resource "helm_release" "cilium" {
  count      = var.cilium ? 1 : 0
  name       = "cilium"
  namespace  = "kube-system"
  repository = "https://helm.cilium.io"
  chart      = "cilium"
  version    = var.versions.cilium
  wait       = true
  timeout    = 900
  values = [yamlencode({
    cni                  = { chainingMode = "aws-cni", exclusive = false }
    enableIPv4Masquerade = false
    routingMode          = "native"
    endpointRoutes       = { enabled = true }
    eni                  = { enabled = false }
    hubble               = { enabled = true, relay = { enabled = true } }
    operator             = { replicas = 1 }
  })]
}

output "kube_dns_ip" { value = local.kube_dns_ip }
