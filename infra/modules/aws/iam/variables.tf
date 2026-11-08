variable "name" {
  description = "Prefix for resource names."
  type        = string
}

variable "oidc_provider_arn" {
  description = "Cluster OIDC provider, the federated principal of every role."
  type        = string
}

variable "oidc_issuer" {
  description = "Cluster OIDC issuer without the scheme."
  type        = string
}

variable "secret_prefix" {
  description = "External Secrets may read only secrets under this prefix."
  type        = string
  default     = "incidentlens/"
}

variable "loki_retention_days" {
  description = "Age at which Loki chunks are deleted from S3."
  type        = number
  default     = 14
}

variable "aws_region" {
  description = "Region of the Secrets Manager secrets."
  type        = string
}

variable "aws_account_id" {
  description = "Account of the Secrets Manager secrets."
  type        = string
}
