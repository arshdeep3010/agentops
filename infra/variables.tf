variable "aws_region" {
  description = "Region for everything."
  type        = string
  default     = "ap-south-1"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "agentops"
}

variable "create_functions" {
  description = "Create the Lambda functions. Leave false on the first apply: the images must be pushed to ECR first (see infra/README.md)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- GitHub Actions

variable "github_repo" {
  description = "GitHub repository allowed to deploy, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo must look like owner/name."
  }
}

variable "github_branch" {
  description = "Only pushes to this branch can deploy."
  type        = string
  default     = "main"
}

variable "github_sub_prefix" {
  description = "The repo's OIDC subject prefix when GitHub uses immutable subjects, e.g. repo:owner@123/name@456 (gh api repos/OWNER/NAME/actions/oidc/customization/sub)."
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  description = "Set to false if the account already has the GitHub OIDC provider (token.actions.githubusercontent.com)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- the app

variable "llm_mode" {
  description = "fake = the offline recorded replay (free, safe for a public demo). live = real model calls with the key in SSM."
  type        = string
  default     = "fake"

  validation {
    condition     = contains(["fake", "live"], var.llm_mode)
    error_message = "llm_mode must be fake or live."
  }
}

variable "llm_base_url" {
  type    = string
  default = "https://integrate.api.nvidia.com/v1"
}

variable "llm_model" {
  type    = string
  default = "nvidia/nemotron-3-ultra-550b-a55b"
}

variable "max_runs_per_day" {
  description = "Runs anyone can start per 24 hours, across all visitors (0 = no limit)."
  type        = number
  default     = 30
}

variable "sweep_minutes" {
  description = "How often a scheduled worker checks for work a missed kick or a dead function left behind (0 = off). Each sweep wakes the database."
  type        = number
  default     = 15
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# ---------------------------------------------------------------- cost guard

variable "budget_email" {
  description = "Email for a monthly AWS budget alert (empty = no budget)."
  type        = string
  default     = ""
}

variable "budget_usd" {
  description = "Monthly budget in USD; you get an email at 80% of it."
  type        = number
  default     = 5
}
