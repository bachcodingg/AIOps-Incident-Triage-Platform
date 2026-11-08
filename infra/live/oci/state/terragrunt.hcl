# Bootstrap: run once. Its own state stays local.
include "oci" {
  path   = find_in_parent_folders("oci.hcl")
  expose = true
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/oci/state"
}

inputs = {
  compartment_ocid = include.oci.locals.compartment_ocid
}
