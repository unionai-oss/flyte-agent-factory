variable "bucket_name" {
  description = "Globally unique name of the S3 bucket."
  type        = string
}

variable "tags" {
  description = "Tags to apply to the bucket."
  type        = map(string)
  default     = {}
}
