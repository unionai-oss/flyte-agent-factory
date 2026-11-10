"""The repository's CI runs every test it holds: the slow lane gets the Terraform the benchmark pins
and the Playwright MCP the browser image pins."""

import re
from pathlib import Path
from typing import Any

import yaml

from flyte_agent_factory import _images, _scanners

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/checks.yml"


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return jobs


def _slow_lane_install(name: str = "Install Terraform") -> str:
    steps = _jobs()["tests-slow"]["steps"]
    install = [s for s in steps if s.get("name") == name]
    assert len(install) == 1, (
        f"the slow lane has no {name!r} step, so the tests that need it would skip or fail"
    )
    # Only the slow job installs it, and there unconditionally.
    assert "if" not in install[0]
    assert not [s for s in _jobs()["tests-fast"]["steps"] if s.get("name") == name]
    return str(install[0]["run"])


def test_the_slow_lane_installs_the_terraform_the_benchmark_pins():
    run = _slow_lane_install()
    assert f"terraform_{_images.TERRAFORM_VERSION}_linux_amd64.zip" in run
    assert f"{_images.TERRAFORM_SHA256}  " in run and "sha256sum --check" in run


def test_the_slow_lane_installs_the_playwright_mcp_the_images_pin():
    run = _slow_lane_install("Install Playwright MCP and Chromium")
    assert re.findall(r"@playwright/mcp@(\S+)", run) == [_images.PLAYWRIGHT_MCP_VERSION]
    assert "install-deps chromium" in run and "install chromium" in run
    # The tests run with a temporary XDG_CACHE_HOME each, so the browser must be found by an explicit path.
    assert 'export PLAYWRIGHT_BROWSERS_PATH="$RUNNER_TEMP/ms-playwright"' in run
    assert 'echo "PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH" >> "$GITHUB_ENV"' in run


def _pytest_marker(job: str) -> str:
    [run] = [str(s["run"]) for s in _jobs()[job]["steps"] if "pytest" in str(s.get("run", ""))]
    [marker] = re.findall(r'pytest tests -q -m "([^"]+)"$', run)
    return str(marker)


def test_both_lanes_together_select_every_test():
    assert sorted(_pytest_marker(job) for job in ("tests-fast", "tests-slow")) == ["not slow", "slow"]


def test_the_check_run_names_the_deployer_and_owner_see_are_kept():
    jobs = _jobs()
    names = {job: jobs[job]["name"] for job in ("tests-fast", "tests-slow", "lint")}
    assert names == {"tests-fast": "Tests (not slow)", "tests-slow": "Tests (slow)", "lint": "Lint"}


def test_each_check_job_runs_only_when_its_lane_says_so():
    """Lint is the one job a lane still gates; the test jobs have no lane condition and run every time.

    The standards gate's test rule covers both lanes on every commit, so neither test job is ever skipped.
    """
    jobs = _jobs()
    for job in ("tests-fast", "tests-slow"):
        assert "if" not in jobs[job] and "needs" not in jobs[job]
    assert jobs["lint"]["needs"] == "changes"
    assert jobs["lint"]["if"] == "needs.changes.outputs.lint == 'true'"
    assert jobs["changes"]["outputs"] == {"lint": "${{ steps.lanes.outputs.lint }}"}
    run = next(s["run"] for s in jobs["changes"]["steps"] if s.get("id") == "lanes")
    assert "python3 .github/ci_lanes.py" in run and '>> "$GITHUB_OUTPUT"' in run
    # A pull request against its merge base, a push against the previous main head, anything else in full.
    assert '--base "$(git rev-parse HEAD^1)"' in run and '--base "$BEFORE"' in run and "--all" in run
    assert jobs["changes"]["steps"][0]["with"]["fetch-depth"] == 2


def test_superseded_pull_request_runs_are_cancelled_but_main_runs_never_are():
    # A cancelled check-run is failing in `_github.commit_health`: the deployer would skip that main commit.
    workflow = yaml.safe_load(WORKFLOW.read_text())
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    group = concurrency["group"]
    assert "github.event.pull_request.number" in group and "github.run_id" in group


def test_main_pushes_still_run_and_a_schedule_runs_everything():
    triggers = yaml.safe_load(WORKFLOW.read_text())[True]  # YAML 1.1 reads the key `on` as True
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in triggers and "workflow_dispatch" in triggers and triggers["schedule"]


def _gate_step() -> dict[str, Any]:
    gate: list[dict[str, Any]] = [
        s for s in _jobs()["standards"]["steps"] if "_gates standards" in str(s.get("run", ""))
    ]
    assert len(gate) == 1
    return gate[0]


def test_the_standards_gate_runs_only_on_the_schedule_or_by_hand():
    """No longer only then: it runs on every pull request and main push, and it blocks (owner, 2026-11-06).

    The name is kept from when the gate was a weekly, non-blocking job; the assertions are the new rule.
    """
    job = _jobs()["standards"]
    assert job["name"] == "Standards gate"
    # No event filter: it runs for pull requests, main pushes, the schedule and workflow_dispatch alike.
    assert "if" not in job
    # A failing gate fails its check-run: nothing downgrades it to a warning.
    assert "continue-on-error" not in job and "continue-on-error" not in _gate_step()
    run = " ".join(_gate_step()["run"].split())
    assert "set +e" not in run and "|| true" not in run
    # The engine has no standards of its own: the gate is given the workspace template's example set.
    assert "--standards src/flyte_agent_factory/_assets/workspace_template/standards" in run


def test_the_gate_reads_both_test_jobs_junit_instead_of_running_pytest_again():
    jobs = _jobs()
    assert sorted(jobs["standards"]["needs"]) == ["tests-fast", "tests-slow"]
    for job, lane in (("tests-fast", "fast"), ("tests-slow", "slow")):
        steps = jobs[job]["steps"]
        [pytest] = [s for s in steps if "pytest" in str(s.get("run", ""))]
        assert pytest["env"]["PYTEST_ADDOPTS"] == f"--junitxml=${{{{ runner.temp }}}}/junit/{lane}.xml"
        assert any(s.get("run") == 'git rev-parse HEAD > "$RUNNER_TEMP/junit/head"' for s in steps)
        [upload] = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
        assert upload["with"]["name"] == f"junit-{lane}"
        assert upload["with"]["path"] == "${{ runner.temp }}/junit/"
        assert upload["with"]["if-no-files-found"] == "error"
        assert steps.index(pytest) < steps.index(upload)
    steps = jobs["standards"]["steps"]
    [download] = [s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    want = {"pattern": "junit-*", "path": "${{ runner.temp }}/junit", "merge-multiple": True}
    assert download["with"] == want
    assert steps.index(download) < steps.index(_gate_step())
    assert '--pytest-junit "$RUNNER_TEMP/junit"' in _gate_step()["run"]


def test_the_gate_job_installs_every_tool_its_rules_need_at_the_image_pins():
    # A missing tool is `tool unavailable`, which blocks; the benchmark fixtures' .tf files select the
    # Terraform pack.
    steps = _jobs()["standards"]["steps"]
    runs = {s.get("name"): str(s.get("run", "")) for s in steps}
    assert f"terraform_{_images.TERRAFORM_VERSION}_linux_amd64.zip" in runs["Install Terraform"]
    assert f"{_images.TERRAFORM_SHA256}  " in runs["Install Terraform"]
    # tflint at the image's pin: the installer takes it, and verifies its checksum, from `_scanners`.
    assert "tflint" in _scanner_steps("standards")
    assert "shellcheck" in runs["Install shellcheck"]
    for name in ("Install Terraform", "Install shellcheck"):
        assert "sha256sum --check" in runs[name]
    assert "uv sync --frozen" in [s.get("run") for s in steps]
    # Two commits: the module-size rule measures growth against the one before.
    assert steps[0]["with"]["fetch-depth"] == 2


def test_lint_runs_nothing_the_standards_gate_already_runs():
    # The gate runs ruff and shellcheck over the whole tree except `.claude-flyte/`, which is all Lint keeps.
    runs = [str(s.get("run", "")) for s in _jobs()["lint"]["steps"]]
    assert not [r for r in runs if "ruff" in r]
    [shellcheck] = [r for r in runs if "xargs" in r]
    assert shellcheck == "git ls-files -z '.claude-flyte/*.sh' | xargs -0 --no-run-if-empty shellcheck"


def _required(doc: str, heading: str) -> list[str]:
    """The check names a doc's section lists, one bulleted, backquoted name each."""
    text = (WORKFLOW.parents[2] / "docs" / doc).read_text()
    section = text[text.index(heading) :].split("\n## ", 1)[0]
    return re.findall(r"^- `([^`]+)`$", section, re.M)


def test_the_documented_required_checks_are_exactly_the_workflow_jobs():
    names = sorted(job["name"] for job in _jobs().values())
    assert sorted(_required("ci.md", "## What `main` requires")) == names
    assert sorted(_required("branch-protection.md", "## The factory repository itself")) == names
    text = (WORKFLOW.parents[2] / "docs" / "branch-protection.md").read_text()
    assert sorted(re.findall(r'\{"context": "([^"]+)", "app_id": 15368\}', text)) == names


def test_the_fast_lane_gets_the_owner_literal_list_from_a_secret_when_there_is_one():
    steps = _jobs()["tests-fast"]["steps"]
    [step] = [s for s in steps if s.get("name") == "Owner literal list, when the repository has one"]
    assert step["env"] == {"LITERALS": "${{ secrets.FACTORY_OWNER_LITERALS }}"}
    run = step["run"]
    # Only when set, and through a file: the guard reads FACTORY_OWNER_LITERALS as a path.
    assert 'if [ -n "$LITERALS" ]' in run
    assert 'echo "FACTORY_OWNER_LITERALS=$RUNNER_TEMP/owner-literals.txt" >> "$GITHUB_ENV"' in run
    names = [s.get("name") or s.get("run") for s in steps]
    assert names.index(step["name"]) < names.index('pytest -m "not slow"')


def test_the_fast_lane_scans_the_built_public_tree_with_the_owner_literal_list():
    # The log of this step is the evidence that the guard loaded the owner's list and read the whole tree that
    # the cutover publishes, not only the checkout's shareable paths.
    steps = _jobs()["tests-fast"]["steps"]
    [step] = [s for s in steps if s.get("name") == "Scan the public tree"]
    run = step["run"]
    assert 'ops/public_tree.py build "$RUNNER_TEMP/public-tree"' in run
    assert 'ops/scan_public.py "$RUNNER_TEMP/public-tree"' in run
    assert "if" not in step
    names = [s.get("name") or s.get("run") for s in steps]
    for before in ("Install scanners", "Owner literal list, when the repository has one", "uv sync --frozen"):
        assert names.index(before) < names.index(step["name"]), before


def test_ruff_reads_what_the_gate_reads_and_not_other_projects_harness_files():
    # In CI's checkout `.claude-flyte/` is not git-ignored, so the gate's `ruff check .` would reach it.
    import tomllib

    pyproject = tomllib.loads((WORKFLOW.parents[2] / "pyproject.toml").read_text())
    assert ".claude-flyte" in pyproject["tool"]["ruff"]["extend-exclude"]


SCANNERS_CLI = "python3 src/flyte_agent_factory/_scanners.py"
CACHE_ACTION = "actions/cache/{}@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"


def _scanner_steps(job: str) -> list[str]:
    """Check `job` restores, installs and saves its scanners through the cache; return their names."""
    steps = _jobs()[job]["steps"]
    by_name = {s.get("name"): s for s in steps}
    [keys] = [s for s in steps if s.get("id") == "scanners"]
    tools: list[str] = (
        keys["run"].removeprefix(f"{SCANNERS_CLI} cache-key ").removesuffix(' >> "$GITHUB_OUTPUT"').split()
    )
    assert keys["run"] == f'{SCANNERS_CLI} cache-key {" ".join(tools)} >> "$GITHUB_OUTPUT"'
    install = by_name["Install scanners"]
    assert install["run"].splitlines() == [
        f"{SCANNERS_CLI} install --cache ~/.cache/factory-scanners "
        f'--bin "$RUNNER_TEMP/scanners" {" ".join(tools)}',
        'echo "$RUNNER_TEMP/scanners" >> "$GITHUB_PATH"',
    ]
    # A scanner that cannot be obtained fails the job: nothing may let it carry on without one.
    assert "if" not in install and "continue-on-error" not in install
    for tool in tools:
        # The key is the tool, its version and its sha256, from the one pin.
        output = f"${{{{ steps.scanners.outputs.{tool} }}}}"
        want = {"path": f"~/.cache/factory-scanners/{output}", "key": output}
        restore, save = by_name[f"Restore {tool}"], by_name[f"Save {tool}"]
        assert restore["uses"] == CACHE_ACTION.format("restore") and restore["with"] == want
        assert save["uses"] == CACHE_ACTION.format("save") and save["with"] == want
        assert save["if"] == f"steps.{tool}-cache.outputs.cache-hit != 'true'"
        assert restore["id"] == f"{tool}-cache"
        assert steps.index(keys) < steps.index(restore) < steps.index(install) < steps.index(save)
    return tools


def test_every_job_that_runs_a_scanner_restores_it_from_the_cache():
    assert _scanner_steps("tests-fast") == ["gitleaks", "tflint"]
    assert _scanner_steps("tests-slow") == ["gitleaks", "tflint"]
    assert _scanner_steps("standards") == ["tflint"]
    # Before the tests run: the jobs install their scanners first.
    for job in ("tests-fast", "tests-slow"):
        names = [s.get("name") for s in _jobs()[job]["steps"]]
        assert names.index("Install scanners") < next(i for i, n in enumerate(names) if n and "pytest" in n)


def test_no_job_downloads_a_scanner_release_itself():
    text = WORKFLOW.read_text()
    for scanner in _scanners.SCANNERS.values():
        assert scanner.url not in text
        assert f"{scanner.name}/releases" not in text
        assert scanner.sha256 not in text
