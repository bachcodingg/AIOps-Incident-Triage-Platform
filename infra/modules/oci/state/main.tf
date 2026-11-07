# Remote state for the OCI environment, applied once with a local backend.
# Object Storage has its own locking, used by Terraform's native `oci` backend.

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

resource "oci_objectstorage_bucket" "state" {
  #checkov:skip=CKV_OCI_9:Oracle-managed encryption is enough; a customer key would need a paid Vault.
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.this.namespace
  name           = var.bucket_name
  access_type    = "NoPublicAccess"
  versioning     = "Enabled"
  storage_tier   = "Standard"

  object_events_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}
