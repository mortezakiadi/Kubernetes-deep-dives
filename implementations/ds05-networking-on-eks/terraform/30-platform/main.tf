data "terraform_remote_state" "network" {
  backend = "local"
  config  = { path = "${path.module}/../10-network/terraform.tfstate" }
}
data "terraform_remote_state" "clusters" {
  backend = "local"
  config  = { path = "${path.module}/../20-clusters/terraform.tfstate" }
}
locals {
  clusters = data.terraform_remote_state.clusters.outputs.clusters
  vpcs     = data.terraform_remote_state.network.outputs.vpcs
  dns      = data.terraform_remote_state.network.outputs.dns
}

module "main" {
  source       = "./modules/platform"
  providers    = { aws = aws, kubectl = kubectl.main, helm = helm.main }
  project      = var.project
  cluster_name = local.clusters.main.name
  region       = var.regions.main
  vpc_id       = local.vpcs.prod-main.id
  alb_name     = "ds05-main"
}
module "second" {
  source       = "./modules/platform"
  providers    = { aws = aws.second, kubectl = kubectl.second, helm = helm.second }
  project      = var.project
  cluster_name = local.clusters.second.name
  region       = var.regions.second
  vpc_id       = local.vpcs.prod-second.id
  alb_name     = "ds05-second"
  cilium       = true
}

# ---- global name, answered from the nearest healthy region ------------------------------------------------------------------
# One name, two internal load balancers. The answer depends on which region the asking VPC is in (latency routing) and on whether the load
# balancer behind a record has a healthy target (evaluate_target_health). A private zone cannot use Route 53's public health checkers, so the load
# balancer's own target health is the signal.
# The load balancers are created by the controller after the Ingress exists, a minute or two later, so wait for them.
resource "terraform_data" "wait_alb" {
  for_each = { main = [var.regions.main, "ds05-main"], second = [var.regions.second, "ds05-second"] }
  provisioner "local-exec" {
    command = "for i in $(seq 1 60); do aws elbv2 describe-load-balancers --names ${each.value[1]} --region ${each.value[0]} >/dev/null 2>&1 && exit 0; sleep 10; done; echo 'load balancer ${each.value[1]} did not appear'; exit 1"
  }
  depends_on = [module.main, module.second]
}
data "aws_lb" "main" {
  name       = "ds05-main"
  depends_on = [terraform_data.wait_alb]
}
data "aws_lb" "second" {
  provider   = aws.second
  name       = "ds05-second"
  depends_on = [terraform_data.wait_alb]
}
resource "aws_route53_record" "app_main" {
  zone_id        = local.dns.internal_zone_id
  name           = "app.${local.dns.internal_zone}"
  type           = "A"
  set_identifier = "main"
  latency_routing_policy { region = var.regions.main }
  alias {
    name                   = data.aws_lb.main.dns_name
    zone_id                = data.aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
resource "aws_route53_record" "app_second" {
  zone_id        = local.dns.internal_zone_id
  name           = "app.${local.dns.internal_zone}"
  type           = "A"
  set_identifier = "second"
  latency_routing_policy { region = var.regions.second }
  alias {
    name                   = data.aws_lb.second.dns_name
    zone_id                = data.aws_lb.second.zone_id
    evaluate_target_health = true
  }
}

output "albs" {
  value = { main = data.aws_lb.main.dns_name, second = data.aws_lb.second.dns_name }
}
