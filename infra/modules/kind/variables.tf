variable "cluster_name" {
  description = "kind cluster name."
  type        = string
}

variable "config_path" {
  description = "kind cluster config, shared with `make cluster`."
  type        = string
}

variable "kind_command" {
  description = "kind binary. The Makefile passes the one it found or downloaded."
  type        = string
  default     = "kind"
}
