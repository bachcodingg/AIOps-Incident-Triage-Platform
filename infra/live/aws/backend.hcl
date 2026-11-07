# Every AWS unit except `state`, which has to exist before this backend does.
locals {
  aws = read_terragrunt_config(find_in_parent_folders("aws.hcl"))
}

generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    terraform {
      backend "s3" {
        bucket         = "${local.aws.locals.state_bucket}"
        key            = "aws/${path_relative_to_include()}/terraform.tfstate"
        region         = "${local.aws.locals.region}"
        dynamodb_table = "${local.aws.locals.lock_table}"
        encrypt        = true
      }
    }
  EOF
}
