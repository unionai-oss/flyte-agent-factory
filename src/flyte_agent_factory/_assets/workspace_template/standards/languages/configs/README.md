# Language tool baselines

Copy the relevant files to the project root and merge existing settings rather
than replacing them. These are review baselines, not runtime dependencies.

- Python: copy `ruff.toml` and `mypy.ini`; install Ruff and mypy in the locked
  development dependencies. Run `python -m mypy src`, adjusting the source path
  to the existing package layout. Add dependency stubs where required rather
  than disabling checks globally.
- TypeScript: copy `eslint.config.mjs`, `tsconfig.json` and `prettier.json`
  (as `.prettierrc.json`). Install ESLint, typescript-eslint, TypeScript and
  Prettier as locked development dependencies. The flat ESLint config uses
  typescript-eslint's typed strict preset and project service; use Node 20.11+
  for `import.meta.dirname`. Merge the project's existing module, target and
  framework settings into tsconfig. Include every linted TypeScript file in a
  tsconfig, including scripts and tests.
- Terraform: copy `tflint.hcl` as `.tflint.hcl`. The bundled Terraform plugin
  checks the recommended rules; add the project's provider plugins with pinned
  versions. Declare Terraform and provider constraints in the project's existing
  `required_version` and `required_providers` blocks and commit the generated
  `.terraform.lock.hcl`. Backend and encryption settings depend on the provider
  and must be reviewed in the actual plan.
- Shell: follow `shellcheck.md`; select the interpreter used by each script.
