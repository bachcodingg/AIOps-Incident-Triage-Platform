output "namespace" {
  value = data.oci_objectstorage_namespace.this.namespace
}

output "bucket_name" {
  value = oci_objectstorage_bucket.state.name
}
