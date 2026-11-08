variable "repositories" {
  description = "ECR repositories to create, one per service image."
  type        = set(string)
}

variable "keep_images" {
  description = "How many images to keep per repository."
  type        = number
  default     = 10
}
