variable "compartment_ocid" {
  description = "Compartment of the bucket."
  type        = string
}

variable "bucket_name" {
  description = "Object Storage bucket for Terraform state."
  type        = string
  default     = "incidentlens-tfstate"
}
