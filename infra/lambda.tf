# Four functions from three images. Created once the images exist (var.create_functions).
# After that, GitHub Actions deploys new code with `aws lambda update-function-code`, so
# Terraform ignores image_uri and won't roll a deploy back.

locals {
  repo = { for k, r in aws_ecr_repository.app : k => r.repository_url }

  common_env = {
    SSM_PREFIX   = local.ssm_prefix
    LLM_MODE     = var.llm_mode
    LLM_BASE_URL = var.llm_base_url
    LLM_MODEL    = var.llm_model
    DB_POOL_MIN  = "0"     # a frozen function holds no idle connections
    EVENT_LISTEN = "false" # streams poll instead of holding a LISTEN connection
    RESULTS_DIR  = "/tmp/results"
  }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = toset([local.api_name, local.worker_name, local.sandbox_name, local.preview_name])
  name              = "/aws/lambda/${each.key}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------- API + UI

resource "aws_lambda_function" "api" {
  count         = local.fn
  function_name = local.api_name
  description   = "AgentOps API and UI (FastAPI behind the Lambda Web Adapter)"
  role          = aws_iam_role.fn["api"].arn
  package_type  = "Image"
  image_uri     = "${local.repo["api"]}:latest"
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 900 # a live stream stays open up to STREAM_MAX_S, then the browser reconnects

  environment {
    variables = merge(local.common_env, {
      WORKER_KICK       = "lambda"
      WORKER_FUNCTION   = local.worker_name
      PREVIEW           = "lambda"
      PREVIEW_URL       = aws_lambda_function_url.preview[0].function_url
      ALLOWED_WORKFLOWS = "sdlc"
      MAX_RUNS_PER_DAY  = tostring(var.max_runs_per_day)
      STREAM_MAX_S      = "600"
    })
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.basic]
  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "api" {
  count              = local.fn
  function_name      = aws_lambda_function.api[0].function_name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM" # Server-Sent Events
}

# ---------------------------------------------------------------- worker

resource "aws_lambda_function" "worker" {
  count         = local.fn
  function_name = local.worker_name
  description   = "AgentOps worker: drains the step queue, then exits"
  role          = aws_iam_role.fn["worker"].arn
  package_type  = "Image"
  image_uri     = "${local.repo["worker"]}:latest"
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 900

  environment {
    variables = merge(local.common_env, {
      SANDBOX          = "lambda"
      SANDBOX_FUNCTION = local.sandbox_name
      PREVIEW_BUCKET   = aws_s3_bucket.previews.bucket
      WORKER_FUNCTION  = local.worker_name # hands over to a fresh copy when time runs low
      LEASE_SECONDS    = "30"
    })
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.basic]
  lifecycle {
    ignore_changes = [image_uri]
  }
}

# A kick that fails is not retried: the next kick or the sweep covers it, and a retry
# would only start a second worker.
resource "aws_lambda_function_event_invoke_config" "worker" {
  count                        = local.fn
  function_name                = aws_lambda_function.worker[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 600
}

# ---------------------------------------------------------------- sandbox (runs the gates)

resource "aws_lambda_function" "sandbox" {
  count         = local.fn
  function_name = local.sandbox_name
  description   = "Runs AI-written code for the quality gates: sealed VPC, no permissions"
  role          = aws_iam_role.fn["sandbox"].arn
  package_type  = "Image"
  image_uri     = "${local.repo["sandbox"]}:latest"
  architectures = ["x86_64"]
  memory_size   = 2048 # more memory = more CPU; mypy and coverage are the slow part
  timeout       = 300

  vpc_config {
    subnet_ids         = aws_subnet.sealed[*].id
    security_group_ids = [aws_security_group.sandbox.id]
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.vpc]
  lifecycle {
    ignore_changes = [image_uri]
  }
}

# ---------------------------------------------------------------- preview (open the shop)

resource "aws_lambda_function" "preview" {
  count         = local.fn
  function_name = local.preview_name
  description   = "Serves the original or merged ShopLite: sealed VPC, reads previews from S3"
  role          = aws_iam_role.fn["preview"].arn
  package_type  = "Image"
  image_uri     = "${local.repo["sandbox"]}:latest"
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 30

  image_config {
    command = ["lambda_preview.handler"]
  }

  environment {
    variables = { PREVIEW_BUCKET = aws_s3_bucket.previews.bucket }
  }

  vpc_config {
    subnet_ids         = aws_subnet.sealed[*].id
    security_group_ids = [aws_security_group.preview.id]
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.vpc]
  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "preview" {
  count              = local.fn
  function_name      = aws_lambda_function.preview[0].function_name
  authorization_type = "NONE"
}

# ---------------------------------------------------------------- public URLs

# A function URL with auth NONE still needs a resource policy that lets anyone call it
# through the URL (and, for URLs created since October 2025, InvokeFunction as well, limited
# to calls that come through the URL).
resource "aws_lambda_permission" "url" {
  for_each               = var.create_functions ? toset([local.api_name, local.preview_name]) : toset([])
  statement_id           = "PublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = each.key
  principal              = "*"
  function_url_auth_type = "NONE"
  depends_on             = [aws_lambda_function.api, aws_lambda_function.preview]
}

resource "aws_lambda_permission" "url_invoke" {
  for_each                 = var.create_functions ? toset([local.api_name, local.preview_name]) : toset([])
  statement_id             = "PublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = each.key
  principal                = "*"
  invoked_via_function_url = true
  depends_on               = [aws_lambda_function.api, aws_lambda_function.preview]
}
