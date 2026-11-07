variable "bucket_name" {
  description = "Globally unique name of the Terraform state bucket."
  type        = string
}

variable "lock_table_name" {
  description = "DynamoDB table used for state locking."
  type        = string
  default     = "incidentlens-tf-locks"
}
