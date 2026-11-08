# Bootstrap: run once. Its own state stays local, and a lost copy can be
# re-imported, because the bucket and the table have fixed names.
include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/state"
}

inputs = {
  bucket_name     = include.aws.locals.state_bucket
  lock_table_name = include.aws.locals.lock_table
}
