variable "name" {
  description = "Budget name."
  type        = string
}

variable "alert_email" {
  description = "Where budget alerts go. Passed in from the environment, never committed."
  type        = string
  sensitive   = true
}

variable "alert_thresholds_usd" {
  description = "Actual monthly spend, in USD, at which to send an alert."
  type        = list(number)
  default     = [20, 50]
}
