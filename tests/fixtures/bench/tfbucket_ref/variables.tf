variable "bucket_name" {
  description = "Globally unique name of the S3 bucket."
  type        = string
}

variable "tags" {
  description = "Tags to apply to the bucket."
  type        = map(string)
  default     = {}
}

variable "expiration_days" {
  description = "Number of days before current objects expire."
  type        = number
  default     = 30

  validation {
    condition     = var.expiration_days > 0 && floor(var.expiration_days) == var.expiration_days
    error_message = "expiration_days must be a positive integer."
  }
}
