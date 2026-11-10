"""The auto-deployer's steps, run from the fetched engine tree: `python -m flyte_agent_factory._deploy_child`.

The `factory-deployer` that evaluates a commit runs the previous release. Anything it decided itself
(which profile keys exist, a slot's recipe, which factory environments a profile enables, the child
scripts) would be the previous release's answer for the new code: a key added together with the code
that reads it would be refused, or deployed without what it adds. So the deployer only fetches,
leases and records; this module, from the tree being deployed, reads the profile and deploys it.

    check   read the profile on stdin, strictly; import every environment it would deploy, as each deploy
            child would (`collect_planned_secrets`); print their names, as JSON. The deployer runs it before
            `deploy`, so a tree that cannot define an environment deploys none rather than half
            It also looks the secrets those environments mount up, and exits 3 naming any that do not exist.
    deploy  deploy it: slots, the factory, then the apps; each deploy child reports what it deployed
            (`_deploy.DEPLOY_REPORT_ENV`)

`--tree` is the engine checkout the profile's standards come from; without it the text must already
carry them (the deployer pod's own profile). A failure it understands is written to `--error`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from . import _deploy, _profile
    from ._errors import REMOTE_ERRORS
else:
    from flyte_agent_factory import _deploy, _profile
    from flyte_agent_factory._errors import REMOTE_ERRORS


def load(text: str, where: str, tree: Path | None) -> _profile.Profile:
    return _profile.shipped(text, where, tree) if tree else _profile.parse(text, where)


# What `_deploy.SLOTS_CHILD`, `FACTORY_CHILD` and `APPS_CHILD` import, without Flyte's client or a deploy:
# each prints, as JSON, every environment it defines (with what they depend on) and the names of the
# secrets each mounts. A secret is a `str` or a `flyte.Secret`, whose name is its `key`.
_ENV_SECRETS = """
import json
def collect_secrets(envs):
    found, pending = {}, list(envs)
    while pending:
        env = pending.pop()
        if env.name not in found:
            secrets = env.secrets or []
            if isinstance(secrets, str) or hasattr(secrets, "key"):
                secrets = [secrets]
            found[env.name] = sorted({getattr(secret, "key", secret) for secret in secrets})
            pending.extend(env.depends_on)
    return found
"""

_IMPORT_SLOT = (
    _ENV_SECRETS
    + """
from flyte_agent_factory import agent
print(json.dumps(collect_secrets([agent.env, agent.subagent_env, agent.setup_env])))
"""
)

_IMPORT_FACTORY = (
    _ENV_SECRETS
    + """
from flyte_agent_factory import _deployer, _factory, _factory_mcp
envs = [_factory.env, _factory_mcp.app, _deployer.env]
if _deployer.PROFILE.board or _deployer.PROFILE.github.configured:
    from flyte_agent_factory import _reconcile
    envs.append(_reconcile.env)
if _deployer.PROFILE.github.webhook_secret:
    from flyte_agent_factory import _webhook
    envs.append(_webhook.env)
if _deployer.PROFILE.bench:
    from flyte_agent_factory import _bench_flow
    envs.append(_bench_flow.env)
if _deployer.PROFILE.loop.enabled:
    from flyte_agent_factory import _loop_flow
    envs.append(_loop_flow.env)
print(json.dumps(collect_secrets(envs)))
"""
)

# The slots refer to the personal browser apps, so a profile with identities must be able to define them. This
# runs where `kubernetes` is not installed (the deployer's image): the app's pod template needs it only when
# Flyte serializes it, which `_deploy.APPS_CHILD` does.
_IMPORT_APPS = (
    _ENV_SECRETS
    + """
from flyte_agent_factory import browser
if not browser.identity_apps:
    raise SystemExit("the profile has browser identities, but browser.py does not define their apps")
print(json.dumps(collect_secrets(list(browser.identity_apps.values()))))
"""
)

#: Seconds for one preflight import: a module import, so anything longer is a hang.
_IMPORT_TIMEOUT = 90


def _import_envs(name: str, script: str, src: Path, env: dict[str, str]) -> dict[str, list[str]]:
    """Run an import script; each environment it defines, with the secrets it mounts."""
    env = {**env, "PYTHONPATH": str(src)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            cwd=src,
            capture_output=True,
            text=True,
            timeout=_IMPORT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _deploy.DeployError(f"importing {name} did not finish in {_IMPORT_TIMEOUT} s") from None
    if done.returncode:
        tail = " | ".join(done.stderr.strip().splitlines()[-3:]) or f"exit code {done.returncode}"
        raise _deploy.DeployError(_deploy.redact(f"importing {name} failed: {tail}"))
    try:
        found = json.loads(done.stdout.strip().splitlines()[-1])
        if not (
            isinstance(found, dict)
            and all(isinstance(env, str) and env and isinstance(s, list) for env, s in found.items())
        ):
            raise ValueError(found)
        return {env: [str(secret) for secret in secrets] for env, secrets in found.items()}
    except (IndexError, ValueError):
        raise _deploy.DeployError(f"importing {name} printed no environment-to-secrets map") from None


def collect_planned_secrets(
    profile: _profile.Profile, src: Path, base_env: Mapping[str, str]
) -> dict[str, list[str]]:
    """Import what a deploy of `profile` would deploy, each as its deploy child would; each env's secrets.

    Returns each imported environment with the names of the secrets it mounts. It proves the imports, not
    the plan: the deployer already fails a deploy that did not deploy an environment it planned
    (`_deployer`), and the children may add environments this list does not name. `check` runs it before
    the deployer deploys anything, so a tree that cannot define one environment (a module the deploy image
    lacks, say) deploys none rather than half. Raises DeployError.
    """
    imported: dict[str, list[str]] = {}
    for slot in profile.slots:
        env = _profile.child_env(profile, _deploy.recipe_env(profile, slot.env, base_env))
        imported |= _import_envs(slot.env, _IMPORT_SLOT, src, env)
    factory = _profile.child_env(
        profile, _deploy.factory_env(base_env, profile.backend.project, profile.backend.domain)
    )
    imported |= _import_envs("the factory", _IMPORT_FACTORY, src, factory)
    if _deploy.app_envs(profile):
        imported |= _import_envs("the apps", _IMPORT_APPS, src, factory)
    print(f"imported: {json.dumps(sorted(imported))}", file=sys.stderr, flush=True)
    return imported


def _list_existing_secrets(project: str, domain: str) -> set[str]:
    """Return the names of the secrets that exist in `project`/`domain`. Tests replace this."""
    import flyte
    from flyte import remote

    flyte.init_from_api_key(api_key=os.environ["_UNION_EAGER_API_KEY"], project=project, domain=domain)
    return {secret.name for secret in remote.Secret.listall()}


def find_missing_secrets(
    needed: Mapping[str, list[str]], project: str, domain: str, base_env: Mapping[str, str]
) -> dict[str, list[str]]:
    """Return each secret that `needed` mounts and `project`/`domain` lacks, with the envs needing it.

    `needed` maps environment -> secret names. Empty when none is missing, or when that cannot be told: a
    deploy that cannot look is not refused for it, and says so on stderr.
    """
    if not any(needed.values()):
        return {}
    if "_UNION_EAGER_API_KEY" not in base_env:
        print("secrets not verified: no API key in the environment", file=sys.stderr, flush=True)
        return {}
    try:
        existing = _list_existing_secrets(project, domain)
    except REMOTE_ERRORS as exc:  # any failure to list is "cannot tell", never "none exist"
        print(
            f"secrets not verified: listing them failed ({type(exc).__name__})", file=sys.stderr, flush=True
        )
        return {}
    missing: dict[str, list[str]] = {}
    for env, secrets in sorted(needed.items()):
        for secret in secrets:
            if secret not in existing:
                missing.setdefault(secret, []).append(env)
    if not missing:
        names = sorted({secret for secrets in needed.values() for secret in secrets})
        print(
            f"secrets verified: all {len(names)} present in {project}/{domain}: {', '.join(names)}",
            file=sys.stderr,
            flush=True,
        )
    return dict(sorted(missing.items()))


def _describe_missing_secrets(missing: Mapping[str, list[str]], project: str, domain: str) -> str:
    needs = ", ".join(f"{secret} (needed by {', '.join(envs)})" for secret, envs in missing.items())
    return (
        f"missing Flyte secrets in {project}/{domain}: {needs}. "
        "Create each with `flyte create secret <name>`; "
        "nothing was deployed, and the deployer retries every tick."
    )


def deploy(
    profile: _profile.Profile, src: Path, *, project: str, domain: str, ref: str, base_env: Mapping[str, str]
) -> None:
    """Deploy every slot, then the factory, then the apps, from `src`. Raises on the first stage that fails.

    The apps come last: a new deployer (which can serialize them) is already deployed when that stage fails
    in an older one, and the next tick runs the new one.
    """
    slots = [s.env for s in profile.slots]
    _deploy.check_slots(profile, slots)
    tail: list[str] = []
    if _deploy.deploy_slots(
        profile, src, slots, project=project, domain=domain, ref=ref, base_env=base_env, stderr_tail=tail
    ):
        raise _deploy.make_child_failure(f"slot deploy of {ref} failed", tail)
    env = _deploy.factory_env(base_env, project, domain)
    tail = []
    if _deploy.deploy_factory(profile, src, project=project, domain=domain, env=env, stderr_tail=tail):
        raise _deploy.make_child_failure(f"factory deploy of {ref} failed", tail)
    tail = []
    if _deploy.deploy_apps(profile, src, project=project, domain=domain, env=env, stderr_tail=tail):
        raise _deploy.make_child_failure(f"apps deploy of {ref} failed", tail)


def _from_src(src: Path) -> None:
    # `python -m` falls back to an installed copy when the tree has no such module: that copy must not
    # deploy (or vouch for) a tree it is not. Explicit, not an assert, which PYTHONOPTIMIZE removes.
    for module in (sys.modules[__name__], _deploy, _profile):
        # These engine modules are source files, never namespace packages.
        if not Path(cast(str, module.__file__)).resolve().is_relative_to(src):
            raise _deploy.DeployError(f"imported {module.__file__}, not the engine tree at {src}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m flyte_agent_factory._deploy_child")
    ap.add_argument("step", choices=("check", "deploy"))
    ap.add_argument("--where", required=True, help="where the profile came from, for messages")
    ap.add_argument("--error", type=Path, required=True, help="file to write a failure's reason to")
    ap.add_argument("--src", type=Path, required=True, help="the engine tree's src/, which must be this code")
    ap.add_argument("--tree", type=Path, help="the engine checkout the profile's standards come from")
    ap.add_argument("--project")
    ap.add_argument("--domain")
    ap.add_argument("--ref")
    args = ap.parse_args(argv)
    if args.step == "deploy" and not (args.project and args.domain and args.ref):
        ap.error("deploy needs --project, --domain and --ref")
    src = args.src.resolve()
    try:
        _from_src(src)
        profile = load(sys.stdin.read(), args.where, args.tree)
        if args.step == "check":
            _deploy.check_slots(profile, [s.env for s in profile.slots])
            needed = collect_planned_secrets(profile, src, os.environ)
            project = os.environ.get("FLYTE_MCP_PROJECT", profile.backend.project)
            domain = os.environ.get("FLYTE_MCP_DOMAIN", profile.backend.domain)
            missing = find_missing_secrets(needed, project, domain, os.environ)
            if missing:
                # Not the last-line contract of a success: the deployer reads this line, then the error file.
                print(json.dumps({"missing_secrets": missing}), flush=True)
                message = _describe_missing_secrets(missing, project, domain)
                args.error.write_text(message)
                print(message, file=sys.stderr)
                return _deploy.MISSING_SECRETS_EXIT
            report = os.environ.get(_deploy.STANDARDS_REPORT_ENV)
            if report:
                Path(report).write_text(json.dumps({"configured": bool(profile.captured_standards())}))
            print(json.dumps(_deploy.planned_envs(profile)))
        else:
            deploy(profile, src, project=args.project, domain=args.domain, ref=args.ref, base_env=os.environ)
    except (_profile.ProfileError, _deploy.DeployError, _deploy.Refused) as exc:
        args.error.write_text(str(exc))
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
