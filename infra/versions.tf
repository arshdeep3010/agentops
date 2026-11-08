terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is kept locally in infra/terraform.tfstate (git-ignored). Back it up, or move it to
  # an S3 backend; losing it means Terraform no longer knows about the resources it created.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.me.account_id
  region  = data.aws_region.current.region
  fn      = var.create_functions ? 1 : 0

  # Function names are fixed so IAM policies can name them before the functions exist.
  api_name     = "${var.name}-api"
  worker_name  = "${var.name}-worker"
  sandbox_name = "${var.name}-sandbox"
  preview_name = "${var.name}-preview"
  fn_arn       = "arn:aws:lambda:${local.region}:${local.account}:function"
  ssm_prefix   = "/${var.name}"
}
