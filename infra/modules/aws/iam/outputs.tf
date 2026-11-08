output "role_arns" {
  description = "IRSA role per workload, for the eks.amazonaws.com/role-arn annotation."
  value       = { for name, role in aws_iam_role.irsa : name => role.arn }
}

output "loki_bucket" {
  value = aws_s3_bucket.loki.bucket
}
