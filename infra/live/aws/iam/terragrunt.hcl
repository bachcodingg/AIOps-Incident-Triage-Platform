include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/iam"
}

dependency "cluster" {
  config_path = "../cluster"

  mock_outputs = {
    oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-central-1.amazonaws.com/id/MOCK"
    oidc_issuer       = "oidc.eks.eu-central-1.amazonaws.com/id/MOCK"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  name              = include.aws.locals.name
  oidc_provider_arn = dependency.cluster.outputs.oidc_provider_arn
  oidc_issuer       = dependency.cluster.outputs.oidc_issuer
  aws_region        = include.aws.locals.region
  aws_account_id    = include.aws.locals.account_id
}
