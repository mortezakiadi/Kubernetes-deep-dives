data "terraform_remote_state" "network" {
  backend = "local"
  config  = { path = "${path.module}/../10-network/terraform.tfstate" }
}
locals {
  vpcs = data.terraform_remote_state.network.outputs.cluster_vpcs
}

module "cluster_main" {
  source             = "./modules/cluster"
  name               = "${var.project}-main"
  kubernetes_version = var.kubernetes_version
  node_subnet_ids    = local.vpcs.main.node_subnets
  admin_cidr         = var.admin_cidr
}
module "cluster_second" {
  source             = "./modules/cluster"
  providers          = { aws = aws.second }
  name               = "${var.project}-second"
  kubernetes_version = var.kubernetes_version
  node_subnet_ids    = local.vpcs.second.node_subnets
  admin_cidr         = var.admin_cidr
  # Cilium enforces policy on this cluster (the "sandbox-style" cluster of the design), so the VPC CNI's own policy agent is off.
  network_policy = false
}

# One ENIConfig per zone: it tells the VPC CNI which subnet (and security group) pods use in that zone.
resource "kubectl_manifest" "eniconfig_main" {
  provider = kubectl.main
  count    = length(local.vpcs.main.azs)
  yaml_body = yamlencode({
    apiVersion = "crd.k8s.amazonaws.com/v1alpha1"
    kind       = "ENIConfig"
    metadata   = { name = local.vpcs.main.azs[count.index] }
    spec = {
      subnet         = local.vpcs.main.pod_subnets[count.index]
      securityGroups = [module.cluster_main.cluster_security_group_id]
    }
  })
  depends_on = [module.cluster_main]
}
resource "kubectl_manifest" "eniconfig_tiny" {
  provider = kubectl.main
  yaml_body = yamlencode({
    apiVersion = "crd.k8s.amazonaws.com/v1alpha1"
    kind       = "ENIConfig"
    metadata   = { name = "tiny" }
    spec = {
      subnet         = local.vpcs.main.tiny_pod_subnet
      securityGroups = [module.cluster_main.cluster_security_group_id]
    }
  })
  depends_on = [module.cluster_main]
}
resource "kubectl_manifest" "eniconfig_second" {
  provider = kubectl.second
  count    = length(local.vpcs.second.azs)
  yaml_body = yamlencode({
    apiVersion = "crd.k8s.amazonaws.com/v1alpha1"
    kind       = "ENIConfig"
    metadata   = { name = local.vpcs.second.azs[count.index] }
    spec = {
      subnet         = local.vpcs.second.pod_subnets[count.index]
      securityGroups = [module.cluster_second.cluster_security_group_id]
    }
  })
  depends_on = [module.cluster_second]
}

# One node group per zone, so a node's ENIConfig label can follow its zone. The main cluster gets a fourth, single-node group whose pods use the tiny subnet.
locals {
  groups_main = merge(
    { for i, az in local.vpcs.main.azs : az => { subnet_ids = [local.vpcs.main.node_subnets[i]], eniconfig = az, desired = 1 } },
    { tiny = { subnet_ids = [local.vpcs.main.node_subnets[0]], eniconfig = "tiny", desired = 1 } },
  )
  groups_second = { for i, az in local.vpcs.second.azs : az => { subnet_ids = [local.vpcs.second.node_subnets[i]], eniconfig = az, desired = 1 } }
}
module "nodes_main" {
  source                    = "./modules/nodes"
  cluster_name              = module.cluster_main.name
  name                      = "${var.project}-main"
  groups                    = local.groups_main
  cluster_security_group_id = module.cluster_main.cluster_security_group_id
  depends_on                = [kubectl_manifest.eniconfig_main, kubectl_manifest.eniconfig_tiny]
}
module "nodes_second" {
  source                    = "./modules/nodes"
  providers                 = { aws = aws.second }
  cluster_name              = module.cluster_second.name
  name                      = "${var.project}-second"
  groups                    = local.groups_second
  cluster_security_group_id = module.cluster_second.cluster_security_group_id
  depends_on                = [kubectl_manifest.eniconfig_second]
}

# The two coordinates the next layer needs.
output "clusters" {
  value = {
    main   = { name = module.cluster_main.name, region = var.regions.main }
    second = { name = module.cluster_second.name, region = var.regions.second }
  }
}
