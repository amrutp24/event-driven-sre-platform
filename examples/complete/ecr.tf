# Image repository for the checkout app. scripts/build-image.sh pushes to it
# and scripts/deploy.sh deploys the tag it pushed.

resource "aws_ecr_repository" "checkout" {
  name = "${var.name}/checkout"

  # Every build gets its own tag (the git SHA), so a tag always means one image
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  # Images are rebuilt from apps/checkout, so they don't block a teardown
  force_delete = true

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "checkout" {
  repository = aws_ecr_repository.checkout.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        # A tagStatus "any" rule has to have the highest priority number
        rulePriority = 2
        description  = "Keep the last 20 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      }
    ]
  })
}
