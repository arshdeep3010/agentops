# GitHub Actions signs in to AWS with a short-lived OIDC token: no AWS keys are stored in GitHub.

locals {
  github_oidc_url = "token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://${local.github_oidc_url}"
}

locals {
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

# Only pushes to one branch of one repository can take this role.
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:sub"
      # Older repos send repo:owner/name; newer ones send repo:owner@ID/name@ID.
      values = compact([
        "repo:${var.github_repo}:ref:refs/heads/${var.github_branch}",
        var.github_sub_prefix == "" ? "" : "${var.github_sub_prefix}:ref:refs/heads/${var.github_branch}",
      ])
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-deploy"
  description          = "Assumed by GitHub Actions to push images and update the AgentOps functions"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

# Least privilege: push to these three repositories, update these four functions. Nothing else.
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level permissions
  }

  statement {
    sid = "PushImages"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
      "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = [for r in aws_ecr_repository.app : r.arn]
  }

  statement {
    sid     = "UpdateFunctions"
    actions = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionUrlConfig"]
    resources = [
      "${local.fn_arn}:${local.api_name}",
      "${local.fn_arn}:${local.worker_name}",
      "${local.fn_arn}:${local.sandbox_name}",
      "${local.fn_arn}:${local.preview_name}",
    ]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-agentops"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
