# ---------------------------------------------------------------- merged code for the preview

resource "aws_s3_bucket" "previews" {
  bucket_prefix = "${var.name}-previews-"
  force_destroy = true # it only holds copies of merged demo code
}

resource "aws_s3_bucket_public_access_block" "previews" {
  bucket                  = aws_s3_bucket.previews.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "previews" {
  bucket = aws_s3_bucket.previews.id

  rule {
    id     = "expire-previews"
    status = "Enabled"
    filter {
      prefix = "previews/"
    }
    expiration {
      days = 30
    }
  }
}

# ---------------------------------------------------------------- secrets

# Terraform creates the parameters with a placeholder and never reads or overwrites the value,
# so secrets stay out of the Terraform state and out of this repo. Set them yourself:
#   aws ssm put-parameter --overwrite --type SecureString --name /agentops/database_url --value '...'
# The functions read them at start-up (engine/config.py, SSM_PREFIX).
resource "aws_ssm_parameter" "secret" {
  for_each    = toset(["database_url", "llm_api_key"])
  name        = "${local.ssm_prefix}/${each.key}"
  type        = "SecureString"
  value       = "set-me"
  description = each.key == "database_url" ? "Postgres URL (direct, not a transaction pooler: LISTEN and SKIP LOCKED need a session)" : "Model API key (only used when llm_mode = live)"

  lifecycle {
    ignore_changes = [value]
  }
}
