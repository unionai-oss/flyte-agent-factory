provider "aws" {
  region = "us-east-1"
}

module "bucket" {
  source = "../.."

  bucket_name = "example-tfbucket-change-me"
  tags = {
    Environment = "example"
  }
}

output "bucket_id" {
  description = "Name of the example bucket."
  value       = module.bucket.bucket_id
}
