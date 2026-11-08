# Container images: GitHub Actions builds and pushes them (.github/workflows/deploy.yml).

locals {
  images = toset(["api", "worker", "sandbox"])
}

resource "aws_ecr_repository" "app" {
  for_each             = local.images
  name                 = "${var.name}-${each.key}"
  image_tag_mutability = "MUTABLE" # "latest" moves; deploys pin the commit tag
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep the last 3 images per repository; storage is billed per GB (the main fixed cost).
resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 3 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
      action       = { type = "expire" }
    }]
  })
}
