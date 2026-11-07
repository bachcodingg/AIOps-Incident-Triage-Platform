include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/ci"
}

inputs = {
  name              = include.aws.locals.name
  github_repository = "bachcodingg/AIOps-Incident-Triage-Platform"
  state_bucket_arn  = "arn:aws:s3:::${include.aws.locals.state_bucket}"
  lock_table_arn    = "arn:aws:dynamodb:${include.aws.locals.region}:${include.aws.locals.account_id}:table/${include.aws.locals.lock_table}"
}
