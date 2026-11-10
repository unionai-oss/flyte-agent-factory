# Shared standards

Precedence: brief > project DESIGN.md > standards. A DESIGN.md override must state a reason.
Tool-enforced MUST checks block DONE on failure; judgement and SHOULD findings are advisory. Rule IDs:
ENG-001, SEC-001, PY-001, TF-001, TS-001, SH-001 and BR-001. Keep IDs stable when editing rules.
Retired IDs are never reused: PY-006 (folded into PY-014) and FLY-001, FLY-002 and FLY-003 (folded into ENG-007).
Each rule has a level, Why:, Check: and one Enforced: line (judgement, check <factory:name>, command, lint, forbid or script), and optionally Judged: for the part of a mechanically checked rule that only a reviewer can judge.
A command, lint or script rule may add Timeout: <seconds> for each of its commands; without it a command gets 300 seconds. PY-005 allows 1200, because a real test suite can run for many minutes.
The engine's docs/standards.md is the full format: how a rule is written, every Enforced: kind, and how your IDs map to the factory's built-in checks.

engineering.md covers delivery and maintenance; security.md covers trust boundaries.
brand.md is a neutral template for visual and customer-facing work only. Fill its placeholders.
languages/ holds Python, Terraform, TypeScript and shell packs; configs/ holds tool baselines.
Auto selection matches each pack's detect stanza in the project directory, at startup and review.
Explicit languages replace auto detection. Add a pack as languages/<name>.md with a detect
stanza, unique stable IDs and a matching tool config; then select it explicitly or use auto.

Copy this folder into a private repository, edit it, and set [standards].dir in the deployment
profile to its path. Relative paths resolve beside the profile first, then against the repository
root. include selects base filenames without .md (default engineering and security).
languages is "auto" or a list of pack names. brand is a file path, or empty for inferred branding.
Deployment captures the files; redeploy after editing them. No separate environment override
style is introduced: select the profile with AGENT_FACTORY_PROFILE as for other tables.

Failing project checks, tool-enforced MUST rules, evidenced correctness/completeness findings and scope violations block DONE. Craft, unevidenced findings and SHOULD rules are advisory. Missing tools in the factory image are image defects, reported as tool unavailable with the required rule IDs; checks that do not apply to a project (no git, no test framework, no UI) are reported as not applicable. Neither blocks. Set policy CRAFT_BLOCKING=true to restore blocking craft review. A line starting `Demo kind: pattern` or `Demo kind: showcase` in the brief selects demo standards; demo package launches default to pattern; no other wording makes a project a demo.
