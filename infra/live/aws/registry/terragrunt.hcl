include "aws" {
  path   = find_in_parent_folders("aws.hcl")
  expose = true
}

include "backend" {
  path = find_in_parent_folders("backend.hcl")
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/aws/registry"
}

inputs = {
  repositories = [for s in ["gateway", "search-api", "catalog", "indexer"] : "${include.aws.locals.name}/${s}"]
}
