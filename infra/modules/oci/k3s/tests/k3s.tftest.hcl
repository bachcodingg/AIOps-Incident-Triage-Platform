mock_provider "oci" {
  mock_data "oci_identity_availability_domains" {
    defaults = {
      availability_domains = [{ name = "AD-1" }]
    }
  }

  mock_data "oci_core_images" {
    defaults = {
      images = [{ id = "ocid1.image.oc1..arm" }]
    }
  }
}

variables {
  name             = "test"
  compartment_ocid = "ocid1.compartment.oc1..test"
  ssh_public_key   = "ssh-ed25519 AAAA test"
  admin_cidrs      = ["203.0.113.10/32"]
}

run "stays_inside_the_always_free_tier" {
  command = plan

  assert {
    condition     = oci_core_instance.k3s.shape == "VM.Standard.A1.Flex"
    error_message = "only Ampere A1 shapes are Always Free at this size"
  }

  assert {
    condition     = var.ocpus <= 2 && var.memory_gb <= 12 && var.boot_volume_gb <= 200
    error_message = "the instance is larger than the free tier budgeted in the plan"
  }
}

run "only_web_ports_are_public" {
  command = plan

  assert {
    condition = alltrue([
      for rule in local.ingress_rules : contains([80, 443], rule.port) if rule.source == "0.0.0.0/0"
    ])
    error_message = "SSH and the Kubernetes API must not be open to the internet"
  }

  assert {
    condition     = length(local.ingress_rules) == 4
    error_message = "expected 2 public rules and 2 admin rules for one admin CIDR"
  }
}

run "refuses_open_admin_ports" {
  command = plan

  variables {
    admin_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.admin_cidrs]
}
