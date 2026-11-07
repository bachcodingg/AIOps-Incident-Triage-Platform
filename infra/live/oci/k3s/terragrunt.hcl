include "oci" {
  path   = find_in_parent_folders("oci.hcl")
  expose = true
}

terraform {
  source = "${get_terragrunt_dir()}/../../../modules/oci/k3s"
}

generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    terraform {
      backend "oci" {
        bucket    = "incidentlens-tfstate"
        namespace = "${get_env("OCI_NAMESPACE")}"
        key       = "oci/k3s/terraform.tfstate"
      }
    }
  EOF
}

inputs = {
  name             = include.oci.locals.name
  compartment_ocid = include.oci.locals.compartment_ocid
  ssh_public_key   = get_env("SSH_PUBLIC_KEY")
  admin_cidrs      = [get_env("ADMIN_CIDR")]
}
