# Terraform for the Union dataplane, one per cloud

Read Union.ai's official installation documentation for the **dataplane** (the customer-hosted
data plane that a Union control plane manages; start at https://docs.union.ai and follow the
deployment / installation / "bring your own cloud" / self-managed sections) and write Terraform
that sets up the dataplane infrastructure on **every cloud provider those docs cover**.

## What "good" means here

- **Separate, standalone code per cloud** under `{name}/<cloud>/` (e.g. `{name}/aws/`,
  `{name}/gcp/`, `{name}/azure/`, plus any other provider the docs document). No shared
  super-module, no cross-cloud abstraction layer: someone deploying on one cloud reads one folder.
- **Shippable**: it would pass review at a company that runs Terraform in production. Pinned
  `required_version` and provider versions, input variables with types and descriptions,
  sensible defaults where the docs give them, outputs for everything the Union side needs
  afterwards (the values the docs say to hand to Union or to the Helm/operator install), no
  hard-coded account IDs, regions or secrets, least-privilege IAM matching what the docs require.
- **Compact, not over-complicated**: the smallest set of resources that matches the documented
  requirements. Prefer the clouds' official/community modules only where they clearly reduce code
  (e.g. the standard VPC/EKS modules on AWS) and say why; no speculative knobs, no features the
  docs do not ask for.
- **Faithful to the docs**: every resource traces to a documented requirement. In each folder's
  `README.md`: what it creates, the doc pages it implements (links), prerequisites, `terraform
  init/plan/apply` usage, the outputs and what to do with them next (the Union-side steps from the
  docs), and any documented requirement you deliberately left out and why.
- A top-level `{name}/README.md` listing the clouds covered, linking each folder, and a coverage
  table: documented requirement -> where it is implemented, per cloud.

## How to verify (you cannot apply to real clouds -- there are no cloud credentials here)

- Install Terraform (and `tflint` if you can) under `$HOME` (download the official release zip;
  there is no package manager access as root).
- For every cloud folder: `terraform fmt -check -recursive`, `terraform init -backend=false`,
  `terraform validate` must all pass. Run `tflint` where available.
- Put those commands in `{name}/checks.sh` so the supervisor
  runs them before review.
- Record in `{name}/README.md` exactly what was validated and how, and say plainly that nothing was
  applied to a real account.

## Do not

- Do not invent requirements the docs do not state; when the docs are ambiguous, choose the
  conservative reading and note it in that folder's README.
- Do not sign in to anything or use any credentials.

Also deliver a hosted console and a migration of existing customers.
