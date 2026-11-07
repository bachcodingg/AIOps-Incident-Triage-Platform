variable "name" {
  description = "Prefix for resource names."
  type        = string
}

variable "compartment_ocid" {
  description = "Compartment for everything in this module."
  type        = string
}

variable "ssh_public_key" {
  description = "Public key for the ubuntu user."
  type        = string
}

variable "admin_cidrs" {
  description = "Addresses allowed to reach SSH and the Kubernetes API. Set this to your own IP."
  type        = list(string)

  validation {
    condition     = !contains(var.admin_cidrs, "0.0.0.0/0")
    error_message = "Do not open SSH and the Kubernetes API to the whole internet."
  }
}

variable "ocpus" {
  description = "Ampere A1 OCPUs."
  type        = number
  default     = 2
}

variable "memory_gb" {
  description = "Memory in GB. The observability stack needs most of it."
  type        = number
  default     = 12
}

variable "boot_volume_gb" {
  description = "Boot volume size; Always Free covers 200 GB of block storage in total."
  type        = number
  default     = 100
}

variable "k3s_version" {
  description = "k3s release to install."
  type        = string
  default     = "v1.37.2+k3s1"
}

variable "ubuntu_version" {
  description = "Canonical Ubuntu release of the base image."
  type        = string
  default     = "24.04"
}

variable "vcn_cidr" {
  description = "VCN address range."
  type        = string
  default     = "10.50.0.0/16"
}
