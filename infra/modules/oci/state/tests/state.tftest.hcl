mock_provider "oci" {}

variables {
  compartment_ocid = "ocid1.compartment.oc1..test"
}

run "state_bucket_is_private_and_versioned" {
  command = plan

  assert {
    condition     = oci_objectstorage_bucket.state.access_type == "NoPublicAccess"
    error_message = "state must never be public"
  }

  assert {
    condition     = oci_objectstorage_bucket.state.versioning == "Enabled"
    error_message = "state history must be kept"
  }
}
