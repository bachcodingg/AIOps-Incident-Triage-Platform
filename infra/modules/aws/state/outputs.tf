output "bucket_name" {
  value = aws_s3_bucket.state.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.state.arn
}

output "lock_table_name" {
  value = aws_dynamodb_table.locks.name
}

output "lock_table_arn" {
  value = aws_dynamodb_table.locks.arn
}
