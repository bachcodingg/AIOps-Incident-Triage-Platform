# Shared by every AWS unit: provider, default tags and the environment's values.
locals {
  name   = "incidentlens"
  region = get_env("AWS_REGION", "eu-central-1")
  # From the environment, not get_aws_account_id(): CI validates this file
  # without AWS credentials. The Makefile sets it for real cloud commands.
  account_id = get_env("AWS_ACCOUNT_ID")

  state_bucket = "${local.name}-tfstate-${local.account_id}"
  lock_table   = "${local.name}-tf-locks"
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    provider "aws" {
      region = "${local.region}"

      # `make destroy` checks for leftovers with this tag.
      default_tags {
        tags = {
          Project   = "${local.name}"
          ManagedBy = "terragrunt"
        }
      }
    }
  EOF
}
