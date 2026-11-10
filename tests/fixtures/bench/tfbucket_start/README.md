# S3 bucket module

This module creates one AWS S3 bucket. It has no backend configuration; callers own
state storage and AWS provider configuration. Terraform 1.9.x and the HashiCorp AWS
provider 5.x are required. Applying the module requires AWS credentials and creates
billable resources.

Pass `bucket_name` (a globally unique string) and optionally `tags` (a map of
strings, default `{}`). The outputs are `bucket_id` and `bucket_arn`.

Check the module without creating resources:

```sh
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```
