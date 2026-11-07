include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/cluster"
}

dependency "network" {
  config_path = "../network"

  mock_outputs = {
    private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  name       = include.aws.locals.name
  subnet_ids = dependency.network.outputs.private_subnet_ids
  # For example ADMIN_CIDR=203.0.113.10/32. There is deliberately no default.
  public_access_cidrs = [get_env("ADMIN_CIDR")]
}
