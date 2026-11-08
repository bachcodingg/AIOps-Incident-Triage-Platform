variable "name" {
  description = "EKS cluster name."
  type        = string
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version."
  type        = string
  default     = "1.37"
}

variable "subnet_ids" {
  description = "Private subnets for the control plane ENIs and the nodes."
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "Addresses allowed to reach the public API endpoint. Set this to your own IP."
  type        = list(string)

  validation {
    condition     = !contains(var.public_access_cidrs, "0.0.0.0/0")
    error_message = "Do not open the Kubernetes API to the whole internet."
  }
}

variable "node_instance_types" {
  description = "Spot pools for the node group. Several types make interruptions less likely."
  type        = list(string)
  default     = ["t3.large", "t3a.large", "m5.large", "m5a.large"]
}

variable "node_count" {
  description = "Number of worker nodes."
  type        = number
  default     = 2
}

variable "node_max_count" {
  description = "Upper bound of the node group."
  type        = number
  default     = 3
}

variable "log_retention_days" {
  description = "Retention of the control plane logs."
  type        = number
  default     = 7
}
