variable "name" {
  description = "Prefix for resource names."
  type        = string
}

variable "github_repository" {
  description = "owner/name of the repository allowed to assume the plan role."
  type        = string
}

variable "state_bucket_arn" {
  description = "Terraform state bucket."
  type        = string
}

variable "lock_table_arn" {
  description = "Terraform lock table."
  type        = string
}
