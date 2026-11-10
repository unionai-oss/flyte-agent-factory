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

Versioning is enabled. An enabled lifecycle rule applies to all objects and expires
current objects after `expiration_days` (a positive integer, default 30). In a
versioned bucket this creates delete markers; it does not remove noncurrent
versions. Configure retention of old versions separately if needed.

`examples/basic/main.tf` shows a caller with a seven-day expiration. Change its
bucket name before applying. To validate the example without creating resources:

```sh
cd examples/basic
terraform init -backend=false
terraform validate
```
