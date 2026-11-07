include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/budget"
}

inputs = {
  name        = include.aws.locals.name
  alert_email = get_env("BUDGET_ALERT_EMAIL")
}
