# VPC Lattice through the Gateway API (Design 05, "Services across clusters and accounts"): the service in this cluster is exported
# under a stable name, and callers in other VPCs (even ones with overlapping addresses) reach it through the service network.
variable "account_id" {
  type    = string
  default = ""
}
variable "lattice_allowed_role_arns" {
  description = "IAM roles allowed to call the exported service."
  type        = list(string)
  default     = []
}

locals {
  lattice_on = var.lattice ? 1 : 0
}

# The Gateway API definitions that the controller needs.
data "http" "gateway_api_crds" {
  count = local.lattice_on
  url   = "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/standard-install.yaml"
}
data "kubectl_file_documents" "gateway_api" {
  count   = local.lattice_on
  content = data.http.gateway_api_crds[0].response_body
}
resource "kubectl_manifest" "gateway_api" {
  for_each          = var.lattice ? data.kubectl_file_documents.gateway_api[0].manifests : {}
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
}

# Lattice's own addresses (a managed prefix list) must be allowed to reach the pods.
data "aws_ec2_managed_prefix_list" "lattice" {
  count = local.lattice_on
  name  = "com.amazonaws.${var.region}.vpc-lattice"
}
resource "aws_vpc_security_group_ingress_rule" "lattice" {
  count             = local.lattice_on
  security_group_id = data.aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.lattice[0].id
  ip_protocol       = "-1"
  description       = "VPC Lattice"
}

resource "aws_iam_role" "lattice" {
  count              = local.lattice_on
  name               = "${var.cluster_name}-lattice-controller"
  assume_role_policy = data.aws_iam_policy_document.pods_trust.json
}
resource "aws_iam_role_policy" "lattice" {
  count  = local.lattice_on
  name   = "lattice"
  role   = aws_iam_role.lattice[0].id
  policy = file("${path.module}/../../iam_policy_lattice.json")
}
resource "aws_eks_pod_identity_association" "lattice" {
  count           = local.lattice_on
  cluster_name    = var.cluster_name
  namespace       = "aws-application-networking-system"
  service_account = "gateway-api-controller"
  role_arn        = aws_iam_role.lattice[0].arn
  depends_on      = [aws_eks_addon.pod_identity]
}

resource "helm_release" "lattice" {
  count            = local.lattice_on
  name             = "gateway-api-controller"
  namespace        = "aws-application-networking-system"
  create_namespace = true
  repository       = "oci://public.ecr.aws/aws-application-networking-k8s"
  chart            = "aws-gateway-controller-chart"
  version          = var.versions.lattice
  wait             = true
  timeout          = 600
  values = [yamlencode({
    awsRegion             = var.region
    awsAccountId          = var.account_id
    clusterName           = var.cluster_name
    clusterVpcId          = var.vpc_id
    defaultServiceNetwork = var.lattice_service_network
  })]
  depends_on = [kubectl_manifest.gateway_api, aws_eks_pod_identity_association.lattice, aws_vpc_security_group_ingress_rule.lattice]
}

resource "kubectl_manifest" "gatewayclass" {
  count = local.lattice_on
  yaml_body = yamlencode({
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "GatewayClass"
    metadata   = { name = "amazon-vpc-lattice" }
    spec       = { controllerName = "application-networking.k8s.aws/gateway-api-controller" }
  })
  depends_on = [helm_release.lattice]
}
resource "kubectl_manifest" "gateway" {
  count = local.lattice_on
  yaml_body = yamlencode({
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "Gateway"
    metadata   = { name = var.lattice_service_network, namespace = "aws-application-networking-system" }
    spec = {
      gatewayClassName = "amazon-vpc-lattice"
      listeners        = [{ name = "http", protocol = "HTTP", port = 80, allowedRoutes = { namespaces = { from = "All" } } }]
    }
  })
  depends_on = [kubectl_manifest.gatewayclass]
}
# The route exports the web service under a Lattice name, and the auth policy decides who may call it.
resource "kubectl_manifest" "web_route" {
  count = local.lattice_on
  yaml_body = yamlencode({
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "HTTPRoute"
    metadata   = { name = "web", namespace = "shop" }
    spec = {
      parentRefs = [{ name = var.lattice_service_network, namespace = "aws-application-networking-system", sectionName = "http" }]
      rules      = [{ backendRefs = [{ name = "web", kind = "Service", port = 80 }] }]
    }
  })
  depends_on = [kubectl_manifest.gateway, kubectl_manifest.web_svc]
}
resource "kubectl_manifest" "web_auth" {
  count = local.lattice_on
  yaml_body = yamlencode({
    apiVersion = "application-networking.k8s.aws/v1alpha1"
    kind       = "IAMAuthPolicy"
    metadata   = { name = "web", namespace = "shop" }
    spec = {
      targetRef = { group = "gateway.networking.k8s.io", kind = "HTTPRoute", name = "web" }
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Effect    = "Allow"
          Principal = { AWS = var.lattice_allowed_role_arns }
          Action    = "vpc-lattice-svcs:Invoke"
          Resource  = "*"
        }]
      })
    }
  })
  depends_on = [kubectl_manifest.web_route]
}
