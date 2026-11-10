# Factory compatibility with OSS Flyte 2

The factory cannot be relaunched unchanged on flyte-gke. The task runtime has the necessary core primitives,
but deployment hardcodes Union credentials and remote builds, the child attachment omits an explicit endpoint,
and the MCP app assumes Union ingress authentication. Artifact discovery and artifact-driven automation need
replacements. A first launch from GitHub is feasible without migrating Union run history.

This is a source audit, dated 2026-10-28, for QA to validate against the deployment. It is not a successful
factory migration or a live compatibility test.

## Evidence and scope

- Factory baseline: `ee05242229a61972d4f15ae156085d6e522bae8a`.
- `SDK:` paths below are relative to `/opt/venv/lib/python3.12/site-packages/flyte/`, the installed SDK
  **2.10.7** (`_version.py:21`). SDK presence alone does not establish backend support.
- `OSS:` paths refer to [flyteorg/flyte v2.0.51](https://github.com/flyteorg/flyte/tree/v2.0.51), commit
  `5986692`, the release pinned by flyte-gke. Network access to GitHub worked: the `v2` branch was cloned
  and the release tag fetched and inspected. The branch exists; no `flyte-2` branch was returned by
  `git ls-remote --heads`. Use the release links rather than assuming current `v2` matches the deployment.
- `flyte-gke/` paths refer to the read-only snapshot in
  the owner's workspace, under `history/briefs/factory/flyte-gke-src/`. The source repository name requested as a
  citation prefix is omitted because the existing shareable-file guard forbids that owner-specific literal. Its `flyte-gke/docs/preflight.md:64` describes the installation and
  `flyte-gke/docs/preflight.md:72` starts its feature findings; these were checked against source where possible.
  Its statement that no identity service exists is stale: the release registers one, but does not issue Union API keys.
  That project used SDK **2.12.0**; this factory pins **2.10.x** (`pyproject.toml:12`).
- The supplied live observation is HTTP **401**, OAuth required, at
  `https://<flyte-gke MCP host>/flyte-mcp/mcp`. No credentials are available here, so no authenticated
  live probe of that backend was possible. A 401 establishes an authentication boundary, not tool support.

Verdicts describe OSS v2.0.51 with this factory's usage: **yes** means source implements the primitive;
**no** means a required service/behavior is absent; **partial** means only some usage is supported or a
factory/deployment change is required. None implies a live factory pass.

## SDK gates and proprietary paths

The relevant explicit SDK gate is the remote builder: missing builder task/ImageService becomes a
Union-support error (SDK `_internal/imagebuild/remote_builder.py:164`). It is not fixed by installing
an optional package. Trigger input offloading is different: SDK `_internal/runtime/trigger_serde.py:302`
catches an Unimplemented response and falls back to inline inputs, so an absent zero-trust upload route
alone does not make scheduled triggers unsupported.

The SDK also labels code-changing `flyte fork` as reserved for the Union plugin
(SDK `cli/_rerun.py:19`, SDK `_run.py:1567`); no factory fork/debug invocation was found in src/ops/scripts.
The installed project dependency confirms that `flyte create api-key` is a plugin command:
`.venv/lib/python3.12/site-packages/flyteplugins_union-0.15.5.dist-info/entry_points.txt:6` maps it to
`flyteplugins.union.cli.api_key`, whose `remote/_api_key.py:23` imports proprietary `AppsServiceClient`
(path relative to `.venv/lib/python3.12/site-packages/flyteplugins/union/`). This service is absent from
OSS registration. This inspection did not issue or print an API key. No `union` CLI command or direct
Union Python import was found in the audited factory runtime; the dependency and key-based initialization
are still real Union coupling. SDK `_internal` imports in attachment are a separate private-API risk.

## Feature matrix

| Feature | Where the factory uses it (file:line) | OSS Flyte 2: yes / no / partial | Evidence | Replacement if not supported |
|---|---|---|---|---|
| Conditions: `ask-`, `direct-`, `gcp-login-` | `src/flyte_agent_factory/_conditions.py:44`; asks: `src/flyte_agent_factory/_bridge.py:706`; direction: `_inbox._watch_app_messages`; login: `agent.gcp_bootstrap` | **yes** for create/wait/signal/timeouts | SDK `_condition.py:187` registers through the controller. OSS `actions/k8s/client.go:171` creates condition actions; `runs/service/run_service.go:1153` signals them; `executor/pkg/controller/taskaction_condition.go:47` handles signal and timeout. | Keep attempt-scoped names and existing bridge. Answer through `remote.Condition.signal` or MCP. Union UI/phone delivery is not implied. |
| Condition webhook notifications | `src/flyte_agent_factory/_bridge.py:706` forwards condition options; shared construction: `src/flyte_agent_factory/_conditions.py:44` | **partial** | SDK `_condition.py:69` defines the webhook and `_condition.py:175` passes it. OSS condition creation/reconciliation above stores the condition but has no webhook delivery path; `flyte-gke/docs/preflight.md:80` explicitly records webhooks ignored. No active factory webhook argument was found at these call sites. | Poll/list conditions with the existing operator/MCP tools; do not depend on condition webhook notifications. |
| Apps: browser, personal browser, GitHub webhook | `src/flyte_agent_factory/browser.py:64`, `src/flyte_agent_factory/browser.py:101`; `src/flyte_agent_factory/_webhook.py:246` | **partial** | OSS `manager/cmd/components.go:268` enables internal apps; `app/internal/k8s/app_client.go:612` injects secret metadata and `app/internal/k8s/app_client.go:700` maps container resources. `flyte-gke/k8s/flyte/values.yaml:44` enables internal apps; Knative MCP is deployed at `flyte-gke/k8s/mcp/app/app.py:75`. `requires_auth` is not enforced by the OSS app path; preflight reports GPU/cluster-pool limitations. | Reuse Knative/Kourier and the stack's ingress/proxy pattern. Provision service accounts, routes and browser storage; protect browser and webhook ingress explicitly. CPU/memory support does not establish GPU app support. |
| `flyte.sandbox.create` | `src/sandbox_probe.py:62`; `src/native_child_probe.py:78`; instructions: `_instructions._project_memory` | **partial** | SDK `sandbox/_code_sandbox.py:111` calls `flyte.build`; `sandbox/_code_sandbox.py:312` skips it when `image` is supplied. SDK `_initialize.py:762` forces remote building inside task initialization. OSS has no remote builder (next row). | Supply an existing, locally built `image=` or `.run(image=...)`; change the generated instructions that recommend runtime `packages=` builds. |
| Remote image builder | `src/flyte_agent_factory/_deploy.py:112`, `src/flyte_agent_factory/_deploy.py:134`; sandbox instructions above | **no** | SDK `_internal/imagebuild/remote_builder.py:60` targets the `system` project, `_internal/imagebuild/remote_builder.py:61` production domain; `_internal/imagebuild/remote_builder.py:131` calls ImageService; `_internal/imagebuild/remote_builder.py:164` raises “remote image builder is not enabled”. OSS `manager/cmd/components.go:216` setup and `runs/setup.go:147` service mounting register no ImageService or build-image task. | Build before deploy with the existing `local-podman` builder; use digest image references in task pods and deploy subprocesses. |
| Local Podman image builder | Factory image recipes: `src/flyte_agent_factory/_images.py:301`; hardcoded builder selection: `src/flyte_agent_factory/_deploy.py:112` | **yes** outside task pods | `flyte-gke/k8s/flyte/config.yaml.tmpl:6` selects `local-podman`; `flyte-gke/scripts/up.sh:352` builds the MCP image with it. SDK `_initialize.py:456` honors configured builder. | Use that existing config and registry on the build host. Initially set existing `[deploy] auto = false`; the in-pod self-deployer cannot simply inherit a remote builder that OSS lacks. |
| `flyte.Secret` injection | `agent._credentials_secret`, `agent._secrets`, the `agent.env` task environment; `src/flyte_agent_factory/_webhook.py:253` | **partial** for this deployment | OSS `executor/pkg/webhook/secrets.go:11` creates the secret mutator; apps propagate requests at `app/internal/k8s/app_client.go:612`. flyte-gke's agent instead mounts Secret Manager CSI files (`flyte-gke/k8s/agent/chart/templates/secrets.yaml:9`). That CSI configuration alone does not satisfy arbitrary factory Flyte secret keys. | Populate matching project/domain Flyte secrets or explicitly mount the required CSI files and translate to existing environment bindings. Provision task/app identities and admit the resulting pods. |
| Secret creation | `agent.gcp_bootstrap` (its `store` step); `scripts/export_local_credentials.sh:99` | **yes** in OSS source; deployment untested | SDK `remote/_secret.py:70` initializes the service client. OSS `manager/cmd/components.go:274` registers SecretService; `secret/service/secret_service.go:60` implements it using Kubernetes Secrets. This is distinct from GCP Secret Manager CSI. | Keep the SDK calls if using OSS-managed secrets; check namespace/scoping, webhook admission and refresh visibility. If choosing CSI instead, write-back requires an explicit Secret Manager integration. |
| Scheduled `Trigger` (`FixedRate`/Cron) | `src/flyte_agent_factory/_factory.py:1132`; `src/flyte_agent_factory/_deployer.py:72`; `src/flyte_agent_factory/_reconcile.py:277`; `src/flyte_agent_factory/_loop_flow.py:484`; `src/flyte_agent_factory/_bench_flow.py:1270` | **yes** | SDK `_internal/runtime/trigger_serde.py:20` serializes both schedule types. OSS `runs/setup.go:176` mounts TriggerService, `runs/setup.go:202` starts the scheduler when enabled (default true: `runs/config/config.go:35`); `runs/scheduler/core/schedule_time.go:39` handles fixed rate and `runs/schedule/cron.go:12` parses cron. | Keep scheduled reconciliation and dispatch; check scheduler configuration and a real firing before cutover. Disable auto-deploy until image deployment is ported. |
| Artifact-driven `Trigger` | `src/flyte_agent_factory/voice.py:62`, `src/flyte_agent_factory/voice.py:68` | **no** | OSS `runs/repository/transformers/trigger.go:48` assigns schedule automation only when a schedule exists, otherwise TYPE_NONE. The source has no mounted ArtifactService, and the scheduler is schedule-based. | Leave optional voice retraining undeployed for the first factory launch; invoke its task directly with a `Dir` input if needed. Artifact events require a separate implementation. |
| `remote.Artifact` catalog | Demo publishing: `_demo_checks._publish_demo_artifact`; voice model lookup: `_demo_checks._fetch_voice_model`, `voice.train`; generated instructions: `_instructions._project_memory` | **no** | SDK `remote/_artifact.py:610`, `remote/_artifact.py:683`, `remote/_artifact.py:799` require ArtifactService. OSS `manager/cmd/components.go:216` and `runs/setup.go:147` mount no ArtifactService; generated artifact protobufs are not a serving implementation. | Use existing task `Dir`/`File` outputs, GitHub demo packages and reports with explicit uploaded URIs. Change the generated discovery instructions; disable optional voice catalog lookup. Demo publication currently catches errors (`_demo_checks._publish_demo_artifact`), so core work can continue, but catalog access will fail. |
| `remote.upload_dir` / `upload_file` | `_demo_checks._publish_demo_artifact`; `voice.train`; both APIs taught in `_instructions._project_memory`; `_slow_lane._submit_async` uploads its tree with `upload_file` | **yes** | SDK `remote/_data.py:401` requests a signed upload location; `remote/_data.py:476`, `remote/_data.py:500` expose the two helpers. OSS `dataproxy/service/dataproxy_service.go:65` implements CreateUploadLocation. flyte-gke grants signing permission (`flyte-gke/README.md:187`). | Keep uploads independently of artifact registration. Check backend signed URLs and task access to the resulting GCS URI. |
| Child actions via `child.attach()` | `src/flyte_agent_factory/child.py:60`, `src/flyte_agent_factory/child.py:95`, `src/flyte_agent_factory/child.py:107`; the slow lane attaches through `_slow_lane._attach` | **partial** | SDK `_interactive_run_context.py:101` restores the context; `_internal/controllers/__init__.py:194` creates the Rust controller. Factory calls `create_controller("rust")` without endpoint; SDK `_internal/controllers/remote/_r_controller.py:142` explicitly requires an API-key environment when endpoint is absent. OSS does not supply Union API keys. | Pass the OSS internal endpoint to the existing controller creation after context restore; preserve the parent action/code bundle. Probe child placement and outputs under the same parent run, including retry. Private APIs are version-sensitive, not inherently Union-only. |
| Run reports / `flyte-note` | `src/flyte_agent_factory/_bridge.py:445`; `_session_setup._install_bridge_commands`; status reader: `src/flyte_agent_factory/_loop_flow.py:99` | **yes** | SDK `report/_report.py:174`, `report/_report.py:180` uploads report.html; OSS `executor/pkg/controller/taskaction_controller.go:1213` records its URI; `dataproxy/service/dataproxy_service.go:413` resolves reports for signed reads. | Keep the bridge and dashboard. Validate incremental writes plus operator/MCP report retrieval; signed GCS URLs need backend signing permission. |
| Factory MCP server and caller identity | `src/flyte_agent_factory/_factory_mcp.py:90`, `src/flyte_agent_factory/_factory_mcp.py:93`; `whoami` tool: `src/flyte_agent_factory/_factory_mcp.py:33` | **partial** | SDK `ai/mcp/_flyte_mcp_app.py:477`, `ai/mcp/_flyte_mcp_app.py:492` selects bearer passthrough with requires_auth. flyte-gke uses requires_auth=False and explicit internal initialization (`flyte-gke/k8s/mcp/app/flyte_mcp_server.py:52`, `flyte-gke/k8s/mcp/app/flyte_mcp_server.py:67`) behind OAuth proxy; README `flyte-gke/README.md:166` describes the chain. OSS `runs/service/identity_service.go:59` rejects whoami when forwarded identity is not trusted; stack `flyte-gke/k8s/flyte/values.yaml:67` disables that trust. | Adapt factory app to the existing protected proxy/internal-endpoint pattern, retain the project/domain scope check, and enable required mutation/condition tools explicitly. Remove or replace whoami for this config; do not infer backend caller identity from a proxy login. |
| Union API keys and endpoint selection | Deployment children: `src/flyte_agent_factory/_deploy.py:111`, `src/flyte_agent_factory/_deploy.py:133`; operators: `ops/operate.py:56`, `ops/state_report.py:85`, `ops/wait_deploy.py:22`, `ops/report_tail.py:21`, `ops/wait_event.py:35`, `ops/plan_dry_run.py:31`, `ops/meter_logins.py:36`, `ops/model_eval/remote.py:145`, `ops/probes/planner_smoke.py:74` | **no** for Union key authentication | SDK `_initialize.py:518` decodes a Union API key to configure the client; `_initialize.py:715` still falls back to injected Union keys in pods. OSS service registration has no API-key issuing service. IdentityService **does exist** (`runs/setup.go:159`), but does not provide Union key issuance/authorization. `flyte-gke/README.md:172` explicitly says the API has no authorization. | Initialize through existing profile `[backend] config`/endpoint with `init_from_config` for operators and explicit internal endpoint in pods. Keep API private behind tailnet/network policies. The public MCP OAuth bearer is not a Union API key. |
| Union console URLs, browser bearer injection and hosted endpoints | Profile: `src/flyte_agent_factory/_profile.py:61`, `src/flyte_agent_factory/_profile.py:262`; browser keys: `src/flyte_agent_factory/_local_browser.py:236`; token in the browser MCP config: `_session_setup._write_mcp_config`; instructions: `_instructions._console_auth_note`, `_instructions._recording_note`; probes: `src/browser_probe.py:48`, `src/demo_chain_probe.py:48`, `src/egress_probe.py:43` | **partial** | Console address is configurable, but `src/flyte_agent_factory/_profile.py:267` constructs a particular /v2 route; browser flow expects hosted bearer credentials. `flyte-gke/README.md:172` uses private network access instead. Search of shareable factory src/ops/scripts found no literal `*.unionai.cloud` host; private profiles and historical briefs are separate deployment inputs. | Set existing endpoint/console/signin/MCP profile fields to OSS addresses, or leave optional console fields empty. Remove Union console instructions/token requirements for OSS; use tailnet browser access. Check the OSS console route before advertising run links. |
| Union plugin, private SDK imports and CLI | `pyproject.toml:24`; stale volume comments: `src/flyte_agent_factory/_images.py:188`; private imports: `src/flyte_agent_factory/child.py:60`, `src/flyte_agent_factory/child.py:95`, `src/native_child_probe.py:62`; credential CLI: `scripts/export_local_credentials.sh:99` | **partial** | The plugin is installed, but no active `union` Python import or `union` CLI invocation was found in shareable src/ops/scripts. Factory state uses native primitives (`src/flyte_agent_factory/_state.py:14`). SDK private controller APIs exist as cited above; their names do not prove a proprietary dependency. `flyte create secret` has an OSS SecretService implementation; `flyte create api-key` does not gain OSS support from installing the Union plugin. | Do not attempt to port obsolete Volume/FUSE paths. Check whether the plugin's installed CLI extensions are needed, then remove the unused dependency in a separate implementation change if appropriate. Keep the SDK pinned while fixing/testing attachment. |
| Workspace/checkpoint and queue storage | `src/flyte_agent_factory/_state.py:66`, `src/flyte_agent_factory/_state.py:71`; queue: `src/flyte_agent_factory/_factory.py:458`, `src/flyte_agent_factory/_queue.py:18`; filesystem dependencies: `src/flyte_agent_factory/_images.py:301` | **partial** for migration | Native Checkpoint/Dir state has no Union Volume requirement. flyte-gke configures GCS run/upload storage (`flyte-gke/k8s/flyte/values.yaml.gotmpl:37`); its IAM table `flyte-gke/README.md:187` distinguishes backend and task identities. A backend grant does not give factory pods queue/state bucket access. | Configure existing `[state] uri` with GCS and grant factory service accounts required object access. Launch fresh from GitHub state; Union run output URIs/history/queue are not automatically discoverable by the new backend. |

## Minimum cutover plan

Estimates are engineering time, assuming an operator has tailnet access, registry write access and the necessary GCP
permissions. They exclude waiting for credentials or cluster capacity. Reuse existing profile fields and Flyte APIs.

1. **Prepare the OSS deployment profile and credentials (0.5–1 day).** Set existing backend config, project/domain,
   image registry, state URI, repositories, slots and secret bindings. Begin with `[deploy] auto = false`; leave optional
   voice/browser/webhook components undeployed unless needed. Provision factory task/app accounts, secret injection,
   network admission and GCS state/queue IAM. Closes secret injection/write-back, workspace/queue and endpoint rows.
2. **Port initialization and builds (1–2 days).** Replace the listed API-key initializers with config/internal endpoint
   initialization. Use the stack's local-podman build host to publish factory/MCP images and deploy existing digest
   references; do not run the current remote-building self-deployer. Update sandbox instructions to require prebuilt
   images. Closes API-key, remote/local builder and sandbox rows. Supporting automatic in-pod image builds is additional
   work, not necessary for the first relaunch.
3. **Fix child attachment and remove catalog assumptions (0.5–1 day).** Pass an explicit OSS endpoint to the existing
   controller creation. Keep native task outputs and GitHub demo packages; report uploaded URIs without artifact
   registration and replace artifact-discovery instructions. Keep voice artifact automation off or launch its task
   directly. Closes child, artifact catalog and artifact-driven trigger rows.
4. **Deploy the factory MCP through the existing OAuth proxy (0.5–1 day).** Adapt explicit internal initialization and
   authentication mode from the flyte-gke MCP implementation. Preserve the project/domain scope check,
   host/origin checks; allow its required write/condition tools through the protected route. Remove unsupported whoami
   expectations. Configure existing public URL fields; check console links. Add browser/webhook ingress only if required.
   Closes MCP, app authentication, console and condition-notification delivery rows.
5. **QA the cutover and transfer ownership (0.5–1 day).** On the target SDK pin, deploy a bounded run that exercises a
   child under its parent, all three condition types, secret reads/write-back, report updates/reads, upload helpers,
   checkpoint retry, GCS queue dispatch and one scheduled reconciliation. Exercise MCP OAuth rejection and authenticated
   launch/status/signal. QA should also check the unsupported artifact behavior is explicit. Stop the Union project with
   its existing `/stop` path, confirm it saved/pushed and ended, then launch factory-improvement from the GitHub branch on
   OSS. Do not copy active queue requests or overlap supervisors. This validates all source-only verdicts before the move.

The minimum first migration is roughly **3–6 engineering days**, with optional voice/catalog, full browser integration
and automatic in-pod rebuilding outside that estimate. This document changes no deployment or running project.

## Not verified

- No authenticated live backend/MCP probe was possible: only the supplied 401 observation is available. No factory task,
  app, condition, trigger, report or upload was executed on flyte-gke during this audit.
- The running backend digest/configuration was not independently read from the cluster. Release source and the supplied
  snapshot may differ from what is currently running. The available `v2` branch is not evidence of a deployed upgrade.
- Factory SDK 2.10.7 interoperability with the stack used with SDK 2.12.0, including image/secret cluster routing and
  Rust controller endpoint handling, needs a target-cluster test. Passing the endpoint is a source-based proposed fix.
- Secret key scoping, webhook mutation, CSI versus managed-secret choice, task/app service accounts, admission policies,
  refresh visibility and queue/state GCS permissions have not been tested for factory pods.
- Condition webhook absence is supported by supplied project experience and source inspection, not an observed delivery
  attempt. Union console/phone notification behavior is not provided by basic OSS condition support.
- Knative browser/webhook startup, internal endpoint resolution, persistent browser recordings, public routing, ignored
  GPU/cluster-pool app fields, per-caller authorization, console /v2 links and proxy token handling remain unprobed.
- Scheduler firing/catch-up timing, retry/checkpoint restoration, queue concurrency and cross-run workspace resume remain
  untested on GKE. Native primitives in source do not establish the factory's operational semantics there.
- Catalog and ImageService absence are findings from complete component/service registration and implementation searches;
  generated protocol definitions exist. No request was sent to confirm an Unimplemented response from the deployed binary.
- Private deployment profiles, their actual hosted `*.unionai.cloud` addresses, DNS/tailnet reachability and availability
  of GitHub/model credentials were not inspected. The shareable-code search is not a claim about private configuration.
- Union run history/checkpoints/artifacts cannot be assumed accessible from OSS; no export/import was attempted. A new run
  from GitHub preserves committed work, not unpushed workspace files, session transcripts or historical run identity.
- The installed Union plugin's incidental CLI behavior without Union credentials and removing its dependency were not
  tested. No runtime Volume dependency was found in the audited factory paths.

QA owns acceptance of these findings and the migration. Repository lint/tests check the documentation change's regression
surface; they cannot establish target-backend compatibility.
