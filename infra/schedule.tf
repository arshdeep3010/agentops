# The sweep: every few minutes, start a worker in case a kick was missed or a function died
# mid-step (its lease runs out and this worker reclaims the step). Usually it finds nothing
# and exits in well under a second.

resource "aws_cloudwatch_event_rule" "sweep" {
  count               = var.create_functions && var.sweep_minutes > 0 ? 1 : 0
  name                = "${var.name}-sweep"
  description         = "Start an AgentOps worker to pick up stranded work"
  schedule_expression = "rate(${var.sweep_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "sweep" {
  count = length(aws_cloudwatch_event_rule.sweep)
  rule  = aws_cloudwatch_event_rule.sweep[0].name
  arn   = aws_lambda_function.worker[0].arn
  input = jsonencode({ source = "sweep" })
}

resource "aws_lambda_permission" "sweep" {
  count         = length(aws_cloudwatch_event_rule.sweep)
  statement_id  = "AllowSweep"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweep[0].arn
}

# ---------------------------------------------------------------- cost guard

# A whole-account monthly budget with an email at 80%. (A budget filtered by the Project tag
# would first need that tag activated for cost allocation.)

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}
