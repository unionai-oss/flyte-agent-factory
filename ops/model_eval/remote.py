"""Flyte adapters. Importing the analysis package never contacts a service."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from flyte import TaskTemplate

from flyte_agent_factory._images import agent_image
from flyte_agent_factory.agent import _VERDICT

from .data import save_json, scrub_tokens, split_usage


def check_task() -> TaskTemplate[Any, Any, Any]:
    """Return the mechanical-check task in the agent image.

    `TaskTemplate.override` refuses `image`, and `mechanical.py` cannot import `agent_image`
    (the task container has no `flyte_agent_factory`), so the swap happens here on the operator side.
    """
    from .mechanical import check, env

    # Flyte submits the image built for the task's environment, so the environment carries it.
    env.image = agent_image()
    return dataclasses.replace(check, image=env.image)


def uri(value: Any) -> Any:
    """Return the path of a Flyte file or directory value, or `value` itself if it has none."""
    return getattr(value, "path", value)


def purpose(inputs: dict[str, Any]) -> tuple[str, str]:
    """Return `(purpose, reason)` for a sub-agent call, inferred from the prompt it was given."""
    brief = inputs["brief"]
    if "**planner** between review rounds" in brief:
        return "planner", "planner prompt signature"
    if "**validation contract**" in brief:
        return "contract", "contract prompt signature"
    if "## The conflicts" in brief and "## How to rule" in brief:
        return "arbiter", "arbiter prompt signature"
    if "## Your concern" in brief:
        match = re.search(r"You are (.*?)(?:\n|\. )", brief)
        return "reviewer", "review prompt signature" + (f": {match[1]}" if match else "")
    if (
        brief.startswith("First run `tar xzf repo.tar.gz && cd repo`")
        or not inputs.get("answer_only")
        or inputs.get("role") == "dev"
    ):
        return "delegate", "writable workspace / dev role"
    return "unknown", "no known prompt signature; manual classification required"


async def collect_run(run_name: str, root: str | Path, remote: ModuleType) -> list[Path]:
    """Collect the sub-agent calls of run `run_name` into case records under `root`."""
    parent = remote.Run.get(run_name)
    parent.get_report()
    parent_details = await remote.ActionDetails.get.aio(run_name=run_name, name="a0")
    parent_inputs = dict(await parent_details.inputs())
    project_name = parent_inputs.get("name", "")
    paths: list[Path] = []
    for action in remote.Action.listall(for_run_name=run_name):
        if not str(getattr(action, "task_name", "")).endswith(".subagent"):
            continue
        details = await remote.ActionDetails.get.aio(run_name=run_name, name=action.name)
        inputs = dict(await details.inputs())
        output = await details.outputs()
        text, usage = split_usage(str(output[0]))
        if not usage:
            text, usage = split_usage(text, await asyncio.to_thread(action.get_report))
        kind, inferred = purpose(inputs)
        judge = re.search(r"You are the \*\*(.*?)\*\* reviewer", inputs["brief"])
        round_match = re.search(r"(?:review round |Round )(\d+)", inputs["brief"])
        verdicts = _VERDICT.findall(text)
        count = re.findall(r"BLOCKING\s*:\s*(\d+)", text, re.IGNORECASE)
        case = {
            "id": f"{run_name}/{action.name}",
            "run": run_name,
            "action": action.name,
            "project_name": project_name,
            "purpose": kind,
            "judge": judge[1] if judge else None,
            "round": int(round_match[1]) if round_match else None,
            "inference": inferred,
            "inputs": {k: uri(v) for k, v in inputs.items()},
            "output": text,
            "output_workdir": uri(output[1]) if len(output) > 1 else None,
            "usage": usage,
            "ground_truth": {
                "verdict": verdicts[-1].upper() if verdicts else None,
                "blocking_count": int(count[-1]) if count else None,
                "findings": text if kind == "reviewer" else None,
                "later_disposition": None,
                "patch_file_count": None,
                "applied": None,
                "parent_evidence": [],
                "limitations": (
                    "No reliable finding/action join in report; dispositions and patch application remain "
                    "unknown unless explicitly evidenced."
                ),
            },
        }
        # Never allow a remote name to escape the case store.
        if any(Path(n).name != n or n in (".", "..") for n in (run_name, action.name)):
            raise ValueError("invalid run/action name")
        path = Path(root) / run_name / f"{action.name}.json"
        save_json(path, case)
        paths.append(path)
    collected = [json.loads(path.read_text()) for path in paths]
    for path, case in zip(paths, collected, strict=False):
        if case["purpose"] == "reviewer":
            case["ground_truth"]["findings"] = None
            case["ground_truth"]["verdict"] = None
            case["later_evidence"] = [
                c["output"]
                for c in collected
                if c["purpose"] == "arbiter"
                or (
                    c["purpose"] == "reviewer"
                    and c["round"] is not None
                    and case["round"] is not None
                    and c["round"] > case["round"]
                )
            ]
            save_json(path, case)
    return paths


class Backend:
    """Reads historical runs from Flyte and replays sub-agent calls against candidate settings."""

    def __init__(self, config: dict[str, Any], allow_live: bool = False) -> None:
        """Connect to Flyte with the evaluation API key; `allow_live` permits replays on live slots."""
        import flyte
        from flyte import remote

        from flyte_agent_factory import _factory, _factory_base

        flyte.init_from_api_key(
            api_key=os.environ["_UNION_EAGER_API_KEY"], project=_factory.PROJECT, domain=_factory.DOMAIN
        )
        self.flyte, self.remote, self.factory = flyte, remote, _factory
        self.base = _factory_base  # where SLOTS and PROFILE live; `_factory` does not re-export them
        self.config = config
        self.allow_live = allow_live

    def runs(self, names: list[str] | None, days: int | None = None) -> list[Any]:
        """Return the runs named in `names`, or those of every slot from the last `days` days."""
        if names:
            return [self.remote.Run.get(name) for name in names]
        if days is None:
            raise ValueError("provide run names or --days")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        runs: list[Any] = []
        for slot in self.base.SLOTS:
            runs.extend(
                self.remote.Run.listall(
                    task_name=slot,
                    created_at=self.remote.TimeFilter(after=cutoff),
                    limit=1_000_000,
                )
            )
        return runs

    def live(self) -> list[Any]:
        """Return the runs currently live on the operated slots."""
        return self.factory.operated_live()

    async def meters(self, engine: str) -> list[Any]:
        """Return the current meter readings for `engine`."""
        from flyte_agent_factory import _meter

        if engine == "codex":
            auth = Path(self.config.get("codex_auth", str(Path.home() / ".codex/auth.json"))).read_text()
            return [await _meter.read_codex(auth)]
        # Every task runs on the profile's one work token.
        from ops.meter_logins import measure

        return [await asyncio.to_thread(measure, self.base.PROFILE.secrets.claude_token)]

    async def mechanical(self, case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Apply a case's patch in its recorded workspace and run the project's checks."""
        if result.get("patch_scrubbed"):
            return {"applied": None, "tests_passed": None, "reason": "patch scrubbed"}
        if not result.get("patch") or not case["inputs"].get("workdir"):
            return {"applied": None, "tests_passed": None, "reason": "missing patch or workspace"}
        from flyte.io import Dir

        from .mechanical import run_check

        workdir = Dir(path=case["inputs"]["workdir"])
        if self.config.get("local_mechanical"):
            return await run_check(workdir, result["patch"], case.get("project_name", ""), isolate=True)
        for interruptible in (True, False):
            task = check_task().override(
                interruptible=interruptible,
                timeout=self.flyte.Timeout(max_queued_time=timedelta(minutes=5) if interruptible else None),
            )
            run = await asyncio.to_thread(
                self.flyte.with_runcontext(
                    mode="remote",
                    project=self.factory.PROJECT,
                    domain=self.factory.DOMAIN,
                    disable_run_cache=True,
                ).run,
                task,
                workdir=workdir,
                patch=result["patch"],
                project_name=case.get("project_name", ""),
            )
            await asyncio.to_thread(run.wait, quiet=True)
            try:
                run.raise_for_status()
            except self.flyte.errors.MaxQueuedTimeExceededError:
                if interruptible:
                    continue
                raise
            measured: dict[str, Any] = (await asyncio.to_thread(run.outputs))[0]
            return measured
        raise AssertionError("unreachable: the on-demand attempt returns or raises")

    async def launch(self, inputs: dict[str, Any], setting: dict[str, Any]) -> dict[str, Any]:
        """Launch the sub-agent task on a case's inputs with `setting`'s engine, model and effort, pinned."""
        from flyte.io import Dir

        kwargs = {**inputs, **{k: setting[k] for k in ("engine", "model", "effort")}, "hard_pin": True}
        # Routing belonged to the historical call; a replay must check today's meters and hard pin.
        kwargs.pop("routed", None)
        if isinstance(kwargs.get("workdir"), str):
            kwargs["workdir"] = Dir(path=kwargs["workdir"])
        task = self.remote.Task.get(
            self.config.get("task", f"{self.base.PROFILE.slots[-1].subagent_env}.subagent"),
            auto_version="latest",
        )
        start = time.monotonic()
        for interruptible in (True, False):
            # No budget check here: callers check once per logical call, and a fallback to
            # on-demand after an allowed spot attempt is the same call, not a new launch.
            bounded = task.override(
                interruptible=interruptible,
                timeout=self.flyte.Timeout(max_queued_time=timedelta(minutes=5)),
            )
            run = await asyncio.to_thread(
                self.flyte.with_runcontext(
                    mode="remote",
                    project=self.factory.PROJECT,
                    domain=self.factory.DOMAIN,
                    disable_run_cache=True,
                ).run,
                bounded,
                **kwargs,
            )
            await asyncio.to_thread(run.wait, quiet=True)
            try:
                run.raise_for_status()
            except self.flyte.errors.MaxQueuedTimeExceededError:
                if interruptible:
                    continue
                raise
            break
        output = await asyncio.to_thread(run.outputs)
        text, usage = split_usage(output[0])
        if not usage:
            text, usage = split_usage(text, await asyncio.to_thread(run.get_report))
        patch = None
        patch_files = None
        original_patch = None
        if not kwargs.get("answer_only") and len(output) > 1:
            with tempfile.TemporaryDirectory(prefix="model-eval-patch-") as tmp:
                await output[1].download(tmp)
                patch_path = Path(tmp) / "factory.patch"
                if patch_path.exists():
                    original_patch = patch_path.read_bytes().decode("utf-8")
                    patch = scrub_tokens(original_patch)
                    patch_files = sum(line.startswith("diff --git ") for line in patch.splitlines())
        return {
            "patch": patch,
            "patch_scrubbed": patch != original_patch,
            "patch_file_count": patch_files,
            "run": run.name,
            "wall_s": time.monotonic() - start,
            "output": text,
            "output_workdir": uri(output[1]) if len(output) > 1 else None,
            "usage": usage,
        }


async def discover_models(config: dict[str, Any]) -> list[str]:
    """Return the Codex models the account offers, or the configured list when the lookup fails."""
    from flyte_agent_factory import _meter

    try:
        auth = Path(config.get("codex_auth", str(Path.home() / ".codex/auth.json"))).read_text()
        result = await _meter.read_codex_models(auth)
        # `model/list` answers {"data": [{"model": ...}]}; `read_codex_models` types it only as `object`.
        items = cast(list[dict[str, str]], result.get("data", []))
        return [item["model"] for item in items if item.get("model")]
    except (OSError, _meter.MeterError, ValueError):
        models: list[str] = config.get("codex_models", [])
        return models
