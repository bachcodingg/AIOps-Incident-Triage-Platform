output "plan_role_arn" {
  description = "Store this in the AWS_PLAN_ROLE_ARN repository variable."
  value       = aws_iam_role.plan.arn
}
