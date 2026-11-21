# One core network for every routed VPC (Design 05, "Routing and peering: one core network").
# The policy is one document. Segments are routing domains: a VPC joins the segment named by its "Segment" tag, and two segments exchange routes
# only where a share action says so.
resource "aws_networkmanager_global_network" "this" {
  description = "${var.project} global network"
  tags        = { Name = var.project }
}

resource "aws_networkmanager_core_network" "this" {
  global_network_id   = aws_networkmanager_global_network.this.id
  description         = var.project
  create_base_policy  = true
  base_policy_regions = [var.regions.main, var.regions.second]
  tags                = { Name = var.project }
}

data "aws_networkmanager_core_network_policy_document" "this" {
  core_network_configuration {
    asn_ranges = ["64512-64600"]
    edge_locations {
      location = var.regions.main
      asn      = "64512"
    }
    edge_locations {
      location = var.regions.second
      asn      = "64513"
    }
  }

  segments {
    name                          = "prod"
    require_attachment_acceptance = false
  }
  segments {
    name                          = "nonprod"
    require_attachment_acceptance = false
  }
  segments {
    name                          = "sandbox"
    isolate_attachments           = true
    require_attachment_acceptance = false
  }
  segments {
    name                          = "shared"
    require_attachment_acceptance = false
  }
  segments {
    name                          = "hybrid"
    require_attachment_acceptance = false
  }

  # Shared services reach everything that needs them; production and non-production never reach each other.
  segment_actions {
    action     = "share"
    mode       = "attachment-route"
    segment    = "shared"
    share_with = ["prod", "nonprod", "hybrid"]
  }
  # The data center must reach the production clusters (API server, webhooks), and nothing else.
  segment_actions {
    action     = "share"
    mode       = "attachment-route"
    segment    = "hybrid"
    share_with = ["prod"]
  }

  attachment_policies {
    rule_number     = 100
    condition_logic = "or"
    conditions {
      type = "tag-exists"
      key  = "Segment"
    }
    action {
      association_method = "tag"
      tag_value_of_key   = "Segment"
    }
  }
}

resource "aws_networkmanager_core_network_policy_attachment" "this" {
  core_network_id = aws_networkmanager_core_network.this.id
  policy_document = data.aws_networkmanager_core_network_policy_document.this.json
}

locals {
  attachments = {
    prod-main      = { vpc_arn = module.prod_main.vpc_arn, subnets = module.prod_main.private_subnet_arns, segment = "prod" }
    prod-second    = { vpc_arn = module.prod_second.vpc_arn, subnets = module.prod_second.private_subnet_arns, segment = "prod" }
    nonprod-main   = { vpc_arn = module.nonprod_main.vpc_arn, subnets = module.nonprod_main.private_subnet_arns, segment = "nonprod" }
    shared-main    = { vpc_arn = module.shared_main.vpc_arn, subnets = module.shared_main.private_subnet_arns, segment = "shared" }
    hybrid-second  = { vpc_arn = module.hybrid_second.vpc_arn, subnets = module.hybrid_second.private_subnet_arns, segment = "hybrid" }
    sandbox-second = { vpc_arn = module.sandbox_second.vpc_arn, subnets = module.sandbox_second.private_subnet_arns, segment = "sandbox" }
  }
}

resource "aws_networkmanager_vpc_attachment" "this" {
  for_each        = local.attachments
  core_network_id = aws_networkmanager_core_network.this.id
  vpc_arn         = each.value.vpc_arn
  subnet_arns     = each.value.subnets
  tags            = { Name = "${var.project}-${each.key}", Segment = each.value.segment }
  depends_on      = [aws_networkmanager_core_network_policy_attachment.this]
}
