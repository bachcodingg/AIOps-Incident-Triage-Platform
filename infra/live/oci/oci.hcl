# Shared by every OCI unit. Authentication comes from ~/.oci/config.
locals {
  name             = "incidentlens"
  region           = get_env("OCI_REGION", "eu-frankfurt-1")
  compartment_ocid = get_env("OCI_COMPARTMENT_OCID")
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    provider "oci" {
      region = "${local.region}"
    }
  EOF
}
