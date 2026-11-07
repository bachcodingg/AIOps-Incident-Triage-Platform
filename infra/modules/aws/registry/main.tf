# ECR for the AWS sessions. GHCR stays the default registry on the low-cost path.

resource "aws_ecr_repository" "this" {
  #checkov:skip=CKV_AWS_136:AES-256 is enough for public source code; a KMS key would add cost.
  for_each = var.repositories

  name                 = each.key
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # `make destroy` must not fail on leftover images

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last ${var.keep_images} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.keep_images
      }
      action = { type = "expire" }
    }]
  })
}
