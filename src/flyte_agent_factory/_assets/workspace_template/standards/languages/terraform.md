# languages/terraform

detect: **/*.tf

## TF-001 MUST
Every Terraform module uses terraform.tf, main.tf, variables.tf and outputs.tf, with submodules as chapters, following the standard module structure and the existing style guide.

Why: Every module reads the same way.

Check: Does every module directory contain exactly those four files?

Enforced: judgement

## TF-002 MUST
Every variable has a type and description and every output has a description; resources are named with nouns that don't repeat the resource type; avoid overusing variables and locals; use count and for_each sparingly; and make most inputs optional with sensible defaults.

Why: This is HashiCorp's and Google's current style guidance.

Check: `tflint` passes with the terraform ruleset, including the documented-variables and documented-outputs rules.

Enforced: command tflint

## TF-003 MUST
Keep variable blocks in `variables.tf` and output blocks in `outputs.tf`, each in alphabetical order.

Why: They are the module's contract and are looked up, not read linearly.

Check: Extract block names from each file and compare with their sorted order.

Enforced: judgement

## TF-004 SHOULD
Order `main.tf` the way a person would build it: providers first, then globally used items (random naming suffixes, account/client data sources, locals), then resources and module calls with dependencies before dependents, grouping related resources only where it keeps that flow.

Why: Terraform ignores order but readers do not.

Check: Read `main.tf` top to bottom and flag any reference to a resource defined further down that is not a deliberate grouping.

Enforced: judgement

## TF-005 MUST
Lay out supporting files as: `files/` for static files (named as at their destination), `templates/` for templates (destination name plus `.tmpl`), `modules/` for submodules, `examples/` for usage examples, `tests/` for tests; nothing unrelated to the module.

Why: A predictable layout makes any module navigable at a glance.

Check: List non-`.tf` files in the module; each must sit in the matching directory with the matching naming.

Enforced: judgement

## TF-006 MUST
Declare provider and module versions only in `required_providers` and `version` on module calls; never a `version` argument inside a `provider` block.

Why: Version constraints in provider blocks behave unpredictably across child modules.

Check: grep -n 'version' inside provider blocks.

Enforced: judgement

## TF-007 MUST
Make modules reusable without edits: generate unique names from a `random_id`/`random_string` suffix (optionally overridden by a prefix variable) rather than hardcoded names.

Why: Two copies of a module must be able to run side by side.

Check: Inspect name arguments of resources with globally or account-unique names for a random suffix or prefix input.

Enforced: judgement

## TF-008 SHOULD
Do not use `count` as an on/off switch in modules that are not general-purpose registry-style modules; copy and adapt the module instead.

Why: Switch logic makes most of the code conditional and hard to read for one use case.

Check: grep for `count = var.` / `count = local.` with a boolean condition outside generalized modules.

Enforced: judgement

## TF-009 MUST
Comment the why on meta-arguments (`count`, `for_each`, `lifecycle`, `depends_on`) and on dynamic blocks and non-trivial expressions, using `#` comments only; do not restate what a resource obviously does.

Why: Meta-arguments and dynamic code carry intent the block itself cannot show.

Check: Inspect every meta-argument and dynamic block for a preceding `#` comment; flag `//` and `/* */` comments outside commented-out code.

Enforced: judgement

## TF-010 MUST
Pass provider credentials through provider environment variables or workload identity, never through Terraform variables or literals.

Why: Variables are written to state; environment variables are not.

Check: Inspect provider blocks and variables for credential arguments; confirm the README documents the environment variables instead.

Enforced: judgement

## TF-011 MUST
Do not use provisioners (`local-exec`, `remote-exec`, `file`) or the `external` data source for configuration; use images, cloud-init/userdata, or a scheduler.

Why: Provisioners are not idempotent, handle failure and drift poorly, and can exfiltrate credentials.

Check: grep -rn 'provisioner\|data "external"' --include=*.tf

Enforced: judgement


## TF-012 MUST
Run the Terraform formatter.

Why: Machine checks enforce consistent formatting.

Check: terraform fmt -check -recursive

Enforced: command terraform fmt -check -recursive
