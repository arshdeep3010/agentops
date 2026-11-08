output "app_url" {
  description = "AgentOps: the UI and the API."
  value       = var.create_functions ? aws_lambda_function_url.api[0].function_url : "not created yet: push the images, then apply with create_functions = true"
}

output "preview_url" {
  description = "The shop preview function."
  value       = var.create_functions ? aws_lambda_function_url.preview[0].function_url : null
}

output "github_variables" {
  description = "Set these as repository variables (Settings > Secrets and variables > Actions > Variables)."
  value = {
    AWS_ROLE_ARN = aws_iam_role.deploy.arn
    AWS_REGION   = local.region
  }
}

output "ecr_repositories" {
  value = local.repo
}

output "secrets_to_set" {
  description = "SSM parameters to fill in yourself (Terraform never sees the values)."
  value       = [for p in aws_ssm_parameter.secret : p.name]
}
