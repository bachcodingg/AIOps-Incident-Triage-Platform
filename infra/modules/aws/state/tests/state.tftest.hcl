mock_provider "aws" {}

variables {
  bucket_name = "incidentlens-tfstate-test"
}

run "state_bucket_is_private_and_versioned" {
  command = plan

  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "state history must be kept"
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.state.block_public_acls,
      aws_s3_bucket_public_access_block.state.block_public_policy,
      aws_s3_bucket_public_access_block.state.ignore_public_acls,
      aws_s3_bucket_public_access_block.state.restrict_public_buckets,
    ])
    error_message = "state bucket must block all public access"
  }

  assert {
    condition     = aws_dynamodb_table.locks.hash_key == "LockID"
    error_message = "the S3 backend expects the lock table key to be LockID"
  }

  assert {
    condition     = aws_dynamodb_table.locks.billing_mode == "PAY_PER_REQUEST"
    error_message = "the lock table must not bill for idle capacity"
  }
}
