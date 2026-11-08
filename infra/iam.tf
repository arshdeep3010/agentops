# One role per function, each with only what that function does.

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = toset(["api", "worker", "sandbox", "preview"])
  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Logs, for the two functions outside the VPC.
resource "aws_iam_role_policy_attachment" "basic" {
  for_each   = toset(["api", "worker"])
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Logs plus the network interface Lambda needs to place a function in the VPC.
resource "aws_iam_role_policy_attachment" "vpc" {
  for_each   = toset(["sandbox", "preview"])
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

locals {
  read_secrets = {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}", "arn:aws:ssm:${local.region}:${local.account}:parameter${local.ssm_prefix}/*"]
  }
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = local.read_secrets.sid
    actions   = local.read_secrets.actions
    resources = local.read_secrets.resources
  }
  statement {
    sid       = "KickWorker"
    actions   = ["lambda:InvokeFunction"]
    resources = ["${local.fn_arn}:${local.worker_name}"]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = local.read_secrets.sid
    actions   = local.read_secrets.actions
    resources = local.read_secrets.resources
  }
  statement {
    sid     = "RunGatesAndHandOver"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "${local.fn_arn}:${local.sandbox_name}",
      "${local.fn_arn}:${local.worker_name}",
    ]
  }
  statement {
    sid       = "CopyMergedCodeForPreview"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.previews.arn}/previews/*"]
  }
}

data "aws_iam_policy_document" "preview" {
  statement {
    sid       = "ReadMergedCode"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.previews.arn}/previews/*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.fn["api"].id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.fn["worker"].id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy" "preview" {
  name   = "preview"
  role   = aws_iam_role.fn["preview"].id
  policy = data.aws_iam_policy_document.preview.json
}

# The sandbox role gets nothing beyond logs and its network interface.
