mock_provider "aws" {}

variables {
  name        = "test"
  alert_email = "alerts@example.com"
}

run "alerts_on_actual_spend_at_20_and_50_usd" {
  command = plan

  assert {
    condition     = length(aws_budgets_budget.monthly.notification) == 2
    error_message = "expected one notification per threshold"
  }

  assert {
    condition     = toset([for n in aws_budgets_budget.monthly.notification : n.threshold]) == toset([20, 50])
    error_message = "the alert thresholds changed"
  }

  assert {
    condition     = alltrue([for n in aws_budgets_budget.monthly.notification : n.notification_type == "ACTUAL"])
    error_message = "alert on real spend, not on forecasts"
  }
}
