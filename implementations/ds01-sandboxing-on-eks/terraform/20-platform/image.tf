# The platform image: one image for every service, built for linux/amd64 and pushed to ECR.

resource "aws_ecr_repository" "platform" {
  name                 = "ds01-platform"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # so teardown removes the images with it

  image_scanning_configuration {
    scan_on_push = true
  }
}

locals {
  platform_dir = "${path.module}/../../platform"
  source_files = sort(tolist(fileset(local.platform_dir, "**")))
  source_hash  = substr(sha256(join("", [for f in local.source_files : filesha256("${local.platform_dir}/${f}")])), 0, 12)
  image        = "${aws_ecr_repository.platform.repository_url}:${local.source_hash}"
}

# Needs Docker running on the machine that runs Terraform.
resource "terraform_data" "build_image" {
  triggers_replace = [local.source_hash, aws_ecr_repository.platform.repository_url]

  provisioner "local-exec" {
    command = "${path.module}/../../scripts/build-image.sh ${var.account_id} ${var.region} ${aws_ecr_repository.platform.name} ${local.source_hash}"
  }
}
