include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/network"
}

inputs = {
  name               = include.aws.locals.name
  cluster_name       = include.aws.locals.name
  availability_zones = ["${include.aws.locals.region}a", "${include.aws.locals.region}b"]
}
