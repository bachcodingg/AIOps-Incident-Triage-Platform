variable "name" {
  description = "Prefix for resource names."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name, used in the subnet discovery tags."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC address range. Each AZ gets one public and one private /20 from it."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = "AZs to use. EKS needs at least two."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "EKS requires subnets in at least two availability zones."
  }
}
