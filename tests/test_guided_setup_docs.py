"""Guard setup prerequisites and executable examples against source/credential drift."""

import copy
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def test_queue_state_requirement_reaches_both_personal_profile_users():
    source = read("src/flyte_agent_factory/_factory_base.py")
    assert "if not PROFILE.state.uri:" in source
    guide = read("docs/quickstart.md")
    assert "[state]" in guide and re.search(r'^uri\s*=\s*"\w+://', guide, re.MULTILINE)
    assert "example-claude-personal.toml" in guide and "example-codex-personal.toml" in guide
    for operation in ("ListBucket", "GetObject", "PutObject", "DeleteObject"):
        assert operation in guide
    assert "runtime" in guide and "local" in guide
    assert "storage" in read("skills/agent-factory/references/setup.md").lower()


def test_deploy_environment_bootstrap_is_documented_before_deployment():
    source = read("src/flyte_agent_factory/_deploy.py")
    required = set(re.findall(r'os\.environ\["(_UNION_\w+)"\]', source))
    assert required
    guide = read("docs/quickstart.md")
    for variable in required:
        assert guide.index(f"export {variable}") < guide.index("python ops/deploy_work.py")
        assert f"unset {variable}" in guide
    assert "endpoint:client_id:client_secret:org" in guide
    assert "create api-key" in guide
    # Retired: the setup-only and per-profile skill packages. One public skill is downloaded, not built.
    assert "package_skill.py --setup" not in guide and "--profile mine" not in guide
    assert "ops/bootstrap.py" in guide
    reference = read("skills/agent-factory/references/setup.md")
    assert "_UNION_EAGER_API_KEY" in reference and "[state]" in reference


def inner_script(block):
    """The script a `bash -c '...'` block hands to bash."""
    return shlex.split(block)[2]


def test_chat_headers_use_prompted_variables_and_issue_write_permission():
    guide = read("docs/chat-operator.md")
    assert re.search(r"Issues[^\n]*write", guide)
    blocks = re.findall(r"```bash\n(.*?)```", guide, re.DOTALL)
    headers = [block for block in blocks if "claude mcp add" in block]
    assert len(headers) == 2
    for block in headers:
        assert "read -r -s" in block and "unset " in block and "set +x" in block
        # Pasted into zsh, `read -s -p` is not bash's: the prompt runs in a bash child.
        assert block.startswith("bash -c '") and block.rstrip().endswith("'")
        args = shlex.split(inner_script(block).replace("\\\n", ""))
        assert any(arg.startswith("Authorization: Bearer $") for arg in args)
        for arg in args:
            if arg.startswith("Authorization: Bearer "):
                assert arg.startswith("Authorization: Bearer $")
    quickstart = read("docs/quickstart.md")
    assert "git var GIT_AUTHOR_IDENT" in quickstart
    assert "gh auth setup-git" in quickstart
    assert "https://github.com:you" not in quickstart


SHELL_FENCE = re.compile(r"```(?:bash|sh|shell|zsh|console)\n(.*?)```", re.DOTALL)
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def without_heredoc_bodies(block):
    """The block's lines outside heredocs, whose Python or TOML may legitimately hold `#`."""
    kept, terminator = [], None
    for line in block.splitlines():
        if terminator:
            terminator = None if line.strip() == terminator else terminator
            continue
        kept.append(line)
        start = HEREDOC_START.search(line)
        terminator = start[2] if start else None
    return kept


def guided_markdown():
    names = ["README.md", "docs/quickstart.md", "docs/chat-operator.md", "docs/factory-mcp-setup.md"]
    names += ["docs/setup.md", "docs/github-app.md"]
    names += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "skills/agent-factory").rglob("*.md"))]
    return names


def test_guided_shell_examples_have_no_comments():
    for path in guided_markdown():
        for block in SHELL_FENCE.findall(read(path)):
            for line in without_heredoc_bodies(block):
                assert not re.search(r"^\s*#|\S\s+#", line), f"{path}: {line}"


def test_comment_check_sees_whole_line_and_inline_comments_but_not_heredoc_bodies():
    assert without_heredoc_bodies("# note\nls") == ["# note", "ls"]
    assert without_heredoc_bodies("python - <<'PY'\n# fine\nPY\nls # no") == ["python - <<'PY'", "ls # no"]


def test_quickstart_storage_probe_runs_without_network(tmp_path, monkeypatch):
    from flyte_agent_factory import _profile

    state = tmp_path / "state"
    state.mkdir()
    profile = tmp_path / "profile.toml"
    profile.write_text(
        read("profiles/example-claude-personal.toml") + f'\n[state]\nuri = "{state.as_uri()}"\n'
    )
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(profile))
    monkeypatch.delenv("AGENT_FACTORY_PROFILE_TOML", raising=False)
    monkeypatch.setattr(_profile, "PROFILES_DIR", tmp_path)
    guide = read("docs/quickstart.md")
    code = re.search(r"<<'PYTHON'\n(.*?)\nPYTHON", guide, re.DOTALL)
    assert code
    exec(compile(code[1], "quickstart-storage-probe", "exec"), {})
    assert not list(state.iterdir())


def test_setup_shell_examples_parse_in_bash():
    for path in ("docs/quickstart.md", "docs/chat-operator.md", "docs/factory-mcp-setup.md"):
        for block in re.findall(r"```bash\n(.*?)```", read(path), re.DOTALL):
            ran = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True, timeout=10)
            assert ran.returncode == 0, f"{path}: {ran.stderr}"


def test_documented_mcp_prompts_expand_headers_and_unset_tokens(tmp_path):
    for path in (p for p in guided_markdown() if "claude mcp add" in read(p)):
        for index, block in enumerate(re.findall(r"```bash\n(.*?)```", read(path), re.DOTALL)):
            if "claude mcp add" not in block:
                continue
            match = re.search(r'read -r -s -p "[^"]*" (\w+)', block)
            assert match, f"{path}: {block}"
            variable = match[1]
            output = tmp_path / f"{Path(path).stem}-{index}"
            script = f"claude() {{ printf '%s\\n' \"$@\" > {shlex.quote(str(output))}; }}\nexport -f claude\n"
            script += block + f"\n[[ ! -v {variable} ]]"
            ran = subprocess.run(
                ["bash", "-e", "-c", script],
                input="local-test-value\n",
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert ran.returncode == 0, ran.stderr
            assert "Authorization: Bearer local-test-value" in output.read_text().splitlines()
            assert "local-test-value" not in ran.stdout + ran.stderr
            skipped = tmp_path / f"{Path(path).stem}-{index}-empty"
            script = script.replace(shlex.quote(str(output)), shlex.quote(str(skipped)))
            empty = subprocess.run(
                ["bash", "-e", "-c", script], input="\n", text=True, capture_output=True, timeout=10
            )
            assert empty.returncode != 0 and not skipped.exists(), (
                f"{path}: empty token registered a connector"
            )


def test_no_default_policies_keys_have_a_written_grant():
    for path in ("docs/chat-operator.md", "docs/quickstart.md"):
        text = read(path)
        if "--no-default-policies" in text:
            assert "factory-mcp-setup.md#connecting-a-client" in text
    grant = read("docs/factory-mcp-setup.md")
    assert "create policy" in grant and "--file" in grant
    assert re.search(r"create assignment --creds-subject \S+ --policy \S+", grant)
    assert "<client-id>" not in grant
    assert re.search(r'client_id=\n: "\$\{client_id:\?', grant)


def test_the_documented_admin_grant_is_the_command_the_bootstrap_prints():
    from flyte_agent_factory._bootstrap_base import Settings
    from flyte_agent_factory._bootstrap_keys import build_command

    example = Settings(
        endpoint="flyte.example.com",
        project="my-agents",
        workspace="you/agent-workspace",
        state_uri="s3://my-agent-state/factory/mine",
    )
    grant = build_command(example, "--grant factory-deploy factory-deploy")
    section = read("docs/factory-mcp-setup.md").split("### Grant the key a role", 1)[1]
    assert f"```bash\n{grant}\n```" in section
    skill = read("skills/agent-factory/references/setup.md")
    assert "`--grant factory-deploy`" in skill and "`--grant factory-chat`" in skill


def test_the_docs_say_the_bootstrap_and_skill_links_need_the_engine_on_its_public_main():
    published = re.compile(
        r"resolve only once this change is on `unionai-oss/flyte-agent-factory`\s+`main`", re.MULTILINE
    )
    for path in ("docs/quickstart.md", "docs/install-audit.md"):
        text = read(path)
        assert published.search(text), path
        assert "unionai-oss/flyte-agent-workspace" in text and "does not exist yet" in text, path
        assert "404" in text, f"{path} does not say what fails until then"
    assert "skills/dist/*.zip" in read("docs/install-audit.md")
    assert "ops/bootstrap.py" in read("docs/quickstart.md").split("**Publish the engine first.**", 1)[1]


def test_non_echo_prompts_run_in_a_bash_child_or_avoid_bash_only_flags():
    for path in guided_markdown():
        for block in SHELL_FENCE.findall(read(path)):
            if block.startswith("bash -c '"):
                continue
            assert not re.search(r"\bread\b[^\n]*\s-\w*[sp]\b", block), (
                f"{path}: zsh rejects bash's read -s/-p"
            )


SKILL_PHASES = [
    "## Is there a factory already?",
    "## Establish where to resume",
    "## Prerequisites",
    "## Do what you can yourself",
    "## Hand over one copy-paste command",
    "## Resume from `--status`",
    "## What the bootstrap sets up, in the command's order",
    "## Connect the factory MCP, then verify",
    "## Take over, launch, and finish",
    "## If Flyte or Union is not up",
]

BOOTSTRAP_STEPS = [
    "tools",
    "workspace",
    "template",
    "profile",
    "signin",
    "project",
    "deploy-key",
    "github-app",
    "ruleset",
    "board",
    "engine-token",
    "deploy",
    "mcp-url",
    "connect",
]


def test_setup_skill_steps_follow_the_quickstart_order():
    # The guided setup retired the numbered `--setup` packaging steps; the order pin is now on the skill's
    # phases and on the bootstrap's step names, in both the skill and the quickstart.
    skill = read("skills/agent-factory/references/setup.md")
    quickstart = read("docs/quickstart.md")
    assert re.findall(r"^## .*$", skill, re.MULTILINE) == SKILL_PHASES
    # Both name every bootstrap step, in the bootstrap's order: the one a person pastes back with --status.
    for name, text in (("skill", skill), ("quickstart", quickstart)):
        positions = []
        for step in BOOTSTRAP_STEPS:
            found = re.search(rf"`{re.escape(step)}`", text)
            assert found, f"{name}: bootstrap step {step} is not named"
            positions.append(found.start())
        if name == "quickstart":
            table = quickstart.split("| Step | Plain words", 1)[1].split("Each line of its output", 1)[0]
            assert re.findall(r"^\| `([\w-]+)` \|", table, re.MULTILINE) == BOOTSTRAP_STEPS
        else:
            listed = skill.split("The steps, in the command's order, are:", 1)[1].split(".", 1)[0]
            assert re.findall(r"`([\w-]+)`", listed) == BOOTSTRAP_STEPS
    # Engine-token handling lives only where the token is stored.
    for heading, body in re.findall(r"^(## .*?)$(.*?)(?=^## |\Z)", skill, re.MULTILINE | re.DOTALL):
        if "claude setup-token" in body:
            assert heading == "## Hand over one copy-paste command"


def test_resume_does_not_overwrite_a_profile_or_repeat_an_mcp_registration():
    quickstart = read("docs/quickstart.md")
    chat = read("docs/chat-operator.md")
    assert "profiles/mine.toml" not in quickstart
    assert "[ -e ../my-workspace/profile.toml ] || cp ../my-workspace/profile.example.toml" in quickstart
    assert "cp ../my-workspace/profile.example.toml ../my-workspace/profile.toml" not in re.sub(
        r"\[ -e \.\./my-workspace/profile.toml \] \|\| cp [^\n]*", "", quickstart
    )
    for text in (quickstart, chat, read("skills/agent-factory/references/setup.md")):
        assert "claude mcp list" in text


def test_shell_blocks_that_write_temporary_files_use_mktemp_and_stop_on_failure():
    """A documented block never writes a fixed /tmp path; one that makes a temp file stops at the first
    failed command and removes the file on exit."""
    for path in guided_markdown() + ["docs/secrets.md"]:
        assert "/tmp/" not in read(path), f"{path}: fixed /tmp path"
        for block in SHELL_FENCE.findall(read(path)):
            if "mktemp" in block:
                assert re.search(r"\bset -[a-z]*e", block), f"{path}: temp-file block lacks set -e"
                assert re.search(r"trap '[^']*rm -r?f? ?\"\$\w+\"' EXIT", block), f"{path}: no EXIT trap"


def test_setup_reference_routes_oss_readers_and_labels_union_only_steps():
    """The packaged skill routes OSS Flyte readers and labels Union-only steps without the quickstart."""
    reference = read("skills/agent-factory/references/setup.md")
    oss_audit = "https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/oss-flyte-compat.md"
    assert oss_audit in reference
    assert reference.count("Union-only") >= 3


def test_union_cli_platform_requirement_is_documented_where_the_key_is_created():
    """flyteplugins-union is Linux-only in pyproject; every doc that creates a Union key must say so."""
    pyproject = read("pyproject.toml")
    if "flyteplugins-union" not in pyproject or "sys_platform == 'linux'" not in pyproject:
        return
    for path in ("docs/factory-mcp-setup.md", "docs/quickstart.md", "docs/chat-operator.md"):
        text = read(path)
        if "create api-key" in text:
            assert "Linux" in text and "macOS" in text, f"{path}: Union CLI platform requirement missing"


def test_no_doc_offers_chat_or_mcp_as_a_way_to_set_engine_credentials():
    """Engine credentials are stored locally; chat and create_secret are not the way to set them."""
    paths = guided_markdown() + ["docs/architecture.md", "docs/operating.md"]
    setter = r"create_secret`?(?: and `?delete_secret`?)? (?:can )?(?:set|write)"
    offers = re.compile(rf"token secrets|{setter}", re.I)
    for path in paths:
        for number, line in enumerate(read(path).splitlines(), 1):
            assert not offers.search(line), f"{path}:{number}: {line.strip()}"


def run_policy_grant(
    tmp_path, monkeypatch, payload, *, exists=True, failure="", client_id="test-client", ansi=False
):
    # flyteplugins ships no type information; import_module keeps these names Any for mypy.
    common = "flyteplugins.union.internal.common"
    PolicyIdentifier = importlib.import_module(f"{common}.identifier_pb2").PolicyIdentifier
    PolicyPb2 = importlib.import_module(f"{common}.policy_pb2").Policy
    Policy = importlib.import_module("flyteplugins.union.remote").Policy
    _dict_to_binding = importlib.import_module("flyteplugins.union.remote._policy")._dict_to_binding

    policy = Policy(
        PolicyPb2(
            id=PolicyIdentifier(name="factory-chat", organization="example-org"),
            bindings=[
                _dict_to_binding(
                    {"role": "contributor", "resource": {"project": "test-project", "domain": "development"}},
                    "example-org",
                )
            ],
        )
    )
    actual = policy.to_dict()
    if callable(payload):
        payload(actual)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "exists": exists,
                "policy": actual,
                "output": payload if isinstance(payload, str) else None,
                "failure": failure,
                "ansi": ansi,
            }
        )
    )
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "uv"
    # Render inside the CLI subprocess so the pasted block's terminal overrides take effect.
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['run', 'python']:\n"
        "    os.execv(sys.executable, [sys.executable, *args[2:]])\n"
        f"state = Path({str(state)!r})\n"
        f"with open({str(calls)!r}, 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "data = json.loads(state.read_text())\n"
        "operation = args[4:6]\n"
        "if data['failure'] == ' '.join(operation):\n"
        "    sys.exit(1)\n"
        "if operation == ['get', 'policy']:\n"
        "    if not data['exists']:\n"
        "        sys.exit(1)\n"
        "    if data['output'] is not None:\n"
        "        print(data['output'])\n"
        "    else:\n"
        "        import contextlib, io\n"
        "        from types import SimpleNamespace\n"
        "        from unittest.mock import patch\n"
        "        import click\n"
        "        from flyteplugins.union.cli.policy import get_policy\n"
        "        from flyteplugins.union.internal.common.policy_pb2 import Policy as PolicyPb2\n"
        "        from flyteplugins.union.remote import Policy\n"
        "        from google.protobuf.json_format import ParseDict\n"
        "        policy = Policy(ParseDict(data['policy'], PolicyPb2()))\n"
        "        config = SimpleNamespace(init=lambda **kwargs: None)\n"
        "        rendered = io.StringIO()\n"
        "        with patch.object(Policy, 'get', return_value=policy):\n"
        "            with click.Context(get_policy, obj=config), contextlib.redirect_stdout(rendered):\n"
        "                get_policy.callback(name=args[6], limit=100)\n"
        "        output = rendered.getvalue()\n"
        "        if data['ansi']:\n"
        "            output = '\\x1b[1m' + output + '\\x1b[0m'\n"
        "        sys.stdout.write(output)\n"
        "elif operation == ['create', 'policy']:\n"
        "    if data['exists']:\n"
        "        sys.exit(1)\n"
        "    import yaml\n"
        "    document = yaml.safe_load(Path(args[-1]).read_text())\n"
        "    assert document == {'name': 'factory-chat', 'bindings': [{'role': 'contributor', "
        "'resource': {'project': 'test-project', 'domain': 'development'}}]}\n"
        "    data['exists'] = True\n"
        "    state.write_text(json.dumps(data))\n"
        "elif operation != ['create', 'assignment']:\n"
        "    sys.exit(2)\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FLYTE_CONFIG", "test config.yaml")
    monkeypatch.setenv("FLYTE_PROJECT", "test-project")
    monkeypatch.setenv("FLYTE_DOMAIN", "development")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    blocks = [
        block for block in SHELL_FENCE.findall(read("docs/factory-mcp-setup.md")) if "client_id=" in block
    ]
    assert len(blocks) == 1
    block = blocks[0].replace("client_id=\n", f"client_id={client_id}\n")
    # Each read now imports the installed CLI; sixteen workers can share a four-CPU runner.
    ran = subprocess.run(["bash", "-c", block], text=True, capture_output=True, timeout=60)
    recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    assert not list(tmp_path.glob("tmp.*")), "policy temporary file leaked"
    assert all(call[:4] == ["run", "flyte", "--config", "test config.yaml"] for call in recorded)
    return ran, [call[4:6] for call in recorded]


@pytest.mark.parametrize(
    "payload",
    [
        lambda policy: policy["bindings"][0]["roleId"].update(name="admin"),
        lambda policy: policy["bindings"][0]["resource"].update(project={"name": "other-project"}),
        lambda policy: policy["bindings"][0]["resource"]["project"]["domain"].update(name="production"),
        lambda policy: policy["bindings"][0].pop("resource"),
        lambda policy: policy["bindings"].append(copy.deepcopy(policy["bindings"][0])),
        lambda policy: policy.update(bindings=[]),
        lambda policy: policy["bindings"][0]["roleId"].update(organization="other-org"),
        lambda policy: policy["bindings"][0]["resource"]["project"]["domain"].update(
            organization={"name": "other-org"}
        ),
        lambda policy: policy["id"].update(name="other-policy"),
        "not a policy",
        "[]",
    ],
    ids=[
        "admin",
        "project",
        "domain",
        "unscoped",
        "extra-binding",
        "empty-bindings",
        "role-org",
        "resource-org",
        "name",
        "malformed",
        "empty-output",
    ],
)
def test_policy_grant_rejects_mismatched_existing_policy(tmp_path, monkeypatch, payload):
    ran, calls = run_policy_grant(tmp_path, monkeypatch, payload)
    assert ran.returncode != 0, ran.stdout + ran.stderr
    assert ["create", "assignment"] not in calls
    assert ["create", "policy"] not in calls


def test_policy_grant_block_resumes_when_the_policy_already_exists(tmp_path, monkeypatch):
    """A rerun after a failed assignment skips create, but checks the existing policy before assignment."""
    text = read("docs/factory-mcp-setup.md")
    assert re.search(r'get policy "\$policy" >/dev/null 2>&1 \|\|\n\s+uv run flyte [^\n]*create policy', text)
    ran, calls = run_policy_grant(tmp_path, monkeypatch, None)
    assert ran.returncode == 0, ran.stderr
    assert ["create", "policy"] not in calls
    assert calls[-1] == ["create", "assignment"]


@pytest.mark.parametrize("failure", ["", "create policy", "get policy"])
def test_policy_grant_checks_new_policy_before_assignment(tmp_path, monkeypatch, failure):
    ran, calls = run_policy_grant(tmp_path, monkeypatch, None, exists=False, failure=failure)
    assert ["create", "policy"] in calls
    if failure:
        assert ran.returncode != 0
        assert ["create", "assignment"] not in calls
    else:
        assert ran.returncode == 0, ran.stderr
        assert calls[-1] == ["create", "assignment"]
        assert calls.count(["get", "policy"]) >= 2


@pytest.mark.parametrize("term", ["dumb", "xterm-256color"])
@pytest.mark.parametrize("color", [None, "0", "1"])
@pytest.mark.parametrize("exists", [False, True])
def test_policy_grant_accepts_matching_policy_across_terminal_environments(
    tmp_path, monkeypatch, term, color, exists
):
    monkeypatch.setenv("TERM", term)
    for name in ("FORCE_COLOR", "PY_COLORS", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    if color is not None:
        monkeypatch.setenv("FORCE_COLOR", color)
        monkeypatch.setenv("PY_COLORS", color)
    ran, calls = run_policy_grant(tmp_path, monkeypatch, None, exists=exists)
    assert ran.returncode == 0, ran.stderr
    assert calls[-1] == ["create", "assignment"]
    assert (["create", "policy"] in calls) == (not exists)
    assert calls.count(["get", "policy"]) == 2


@pytest.mark.parametrize("mismatch", [False, True])
def test_policy_grant_checks_scope_even_if_terminal_styles_survive(tmp_path, monkeypatch, mismatch):
    # NO_COLOR still permits bold; parsing must not depend on the caller's terminal settings.
    payload = (lambda policy: policy["bindings"][0]["roleId"].update(name="admin")) if mismatch else None
    ran, calls = run_policy_grant(tmp_path, monkeypatch, payload, ansi=True)
    if mismatch:
        assert ran.returncode != 0
        assert "Policy scope mismatch" in ran.stderr
        assert ["create", "assignment"] not in calls
    else:
        assert ran.returncode == 0, ran.stderr
        assert calls[-1] == ["create", "assignment"]


def test_policy_grant_requires_client_id_before_any_cli_call(tmp_path, monkeypatch):
    ran, calls = run_policy_grant(tmp_path, monkeypatch, None, client_id="")
    assert ran.returncode != 0
    assert not calls


def test_every_readme_step_routes_through_the_operator():
    steps = read("README.md").split("## Using it, in five steps", 1)[1].split("\n## ", 1)[0]
    for number in range(1, 6):
        match = re.search(rf"^{number}\. (.*?)(?=^\d\. |\Z)", steps, re.S | re.M)
        assert match, f"README step {number} is missing"
        assert "operator" in match.group(1), f"README step {number} does not mention the operator"


def test_setup_operator_prepares_filled_in_handoffs_at_the_step_that_needs_them():
    for path in ("docs/quickstart.md", "skills/agent-factory/references/setup.md"):
        text = read(path)
        assert "one copy-paste command" in text
        assert "claude.ai has no local shell" in text
        assert "GitHub file-edit tool" in text
        assert "non-secret" in text and "never" in text.lower()
        if path.startswith("skills/"):
            sections = {
                "GitHub": ("## Do what you can yourself", ("CODEOWNERS", "create_repository")),
                "Profile": (
                    "## What the bootstrap sets up, in the command's order",
                    ("profile.toml", "[repo] project", "[direction]", "never overwritten"),
                ),
                "Deploy": ("## Hand over one copy-paste command", ("factory-deploy", "client_id")),
                "Factory MCP": (
                    "## Connect the factory MCP, then verify",
                    ("grant", "client_id", "factory-chat", "one copy-paste command"),
                ),
            }
            for heading, (start, required) in sections.items():
                section = text.split(start, 1)[1].split("\n## ", 1)[0]
                for value in required:
                    assert value in section, f"{path}: {heading} handoff missing {value}"
        else:
            anchors = {
                "GitHub": ("## 1.", "## 2."),
                "Profile": ("## 3.", "## 4."),
                "Deploy": ("## 5.", "Follow [chat-operator.md"),
                "Factory MCP": ("Follow [chat-operator.md", "## 6."),
            }
            required_by_heading = {
                "GitHub": ("CODEOWNERS", "@OWNER"),
                "Profile": ("filled-in", "[state]", "[backend]", "[repo]", "[secrets]", "never overwrite"),
                "Deploy": ("[mcp] public_url", "only", "one copy-paste command"),
                "Factory MCP": ("grant block", "client_id", "factory-chat", "one copy-paste command"),
            }
            for heading, (start, end) in anchors.items():
                section = text.split(start, 1)[1].split(end, 1)[0]
                for value in required_by_heading[heading]:
                    assert value in section, f"{path}: {heading} handoff missing {value}"
        assert "factory-deploy" in text and "client_id" in text


def test_install_audit_classifies_every_step_and_keeps_external_work_not_run():
    audit = read("docs/install-audit.md").split("## Setup walkthrough verification", 1)[1]
    assert "Chat-done" in audit and "One copy-paste command" in audit and "UI/admin" in audit
    rows = [line.split(" | ") for line in audit.splitlines() if line.startswith("| ")][1:]
    assert len(rows) == 14
    for row in rows:
        assert len(row) == 4, f"Missing handoff/evidence column: {row}"
        assert any(label in row[1] for label in ("Chat-done", "One copy-paste command", "UI/admin"))
        assert "`" in row[2] and "RUN" in row[2], f"Missing runnable/source evidence: {row}"
        assert "NOT RUN" in row[3], f"Missing live limitation and substitute: {row}"
    for value in ("CODEOWNERS", "[state]", "public_url", "client_id"):
        assert any(value in " ".join(row) and "One copy-paste command" in row[1] for row in rows)
    assert "test_setup_operator_prepares_filled_in_handoffs_at_the_step_that_needs_them" in audit
    page = read("docs/install-audit.md")  # the skill-install row sits above the walkthrough section
    assert "agent-factory.zip" in page and "since.zip" in page


APP_ERROR = "the GitHub App (id <your App id>) is not installed on unionai-oss/flyte-agent-factory"
#: What the deployer does where the App is not installed (`_deployer._deploy_reader`); the docs say so.
ANONYMOUS = ("anonymously", "`[direction] repo`")
FIRST_COMMAND = next(
    block.strip()
    for block in re.findall(r"```bash\n(.*?)```", read("docs/quickstart.md"), re.DOTALL)
    if "ops/bootstrap.py --endpoint" in block and "--redeploy" not in block
)
REDEPLOY = FIRST_COMMAND + " --redeploy"
SETUP_FILES = (
    "docs/quickstart.md",
    "docs/github-app.md",
    "skills/agent-factory/references/setup.md",
)


def test_the_public_engine_without_the_app_is_read_anonymously_and_has_one_redeploy_command():
    for path in SETUP_FILES:
        text = " ".join(read(path).split())
        assert APP_ERROR not in text, f"{path}: the deployer no longer fails there; it reads anonymously"
        assert all(words in text for words in ANONYMOUS), path
        assert REDEPLOY in text, path
        assert "ops/deploy_factory.py --profile" not in text, f"{path}: the hand redeploy command is retired"
    skill = " ".join(read("skills/agent-factory/references/setup.md").split())
    # The skill must not "repair" anything by dropping the direction or installing on the engine.
    assert "do not remove `[deploy] auto` or `[direction]`" in skill
    assert "do not ask them to install the App on unionai-oss" in skill


def test_docs_do_not_tell_users_to_fork_the_engine_or_install_the_app_on_it():
    app = " ".join(read("docs/github-app.md").split())
    assert "and the engine repository if you use" not in app
    quick = " ".join(read("docs/quickstart.md").split())
    assert "use a fork and configure `[repo] factory`" not in quick
    assert "factory = the public engine" in quick or "public engine" in quick


def test_owner_onboarding_steps_appear_in_the_owners_order():
    guide = read("docs/quickstart.md")
    section = guide[guide.index("The setup it performs") : guide.index("Around those seven")]
    order = ["Private workspace", "Profile at the workspace root", "Write deploy key", "Your own GitHub App"]
    order += ["Protected main", "Board", "First deploy"]
    positions = [section.index(f"**{name}.**") for name in order]
    assert positions == sorted(positions)
    for words in ("CODEOWNERS", "[repo] project", "[direction]", "manifest flow", "Default repository"):
        assert words in section
    # Honest boundaries stay stated.
    flat = " ".join(guide.split())
    assert "Default repository" in flat and "by hand" in flat
    assert "reads that repository anonymously" in flat


def test_direction_and_auto_are_documented_as_written_only_with_the_app():
    guide = " ".join(read("docs/quickstart.md").split())
    assert "`[direction]` and `[deploy] auto = true`, only when the App exists" in guide
    assert "no `[direction]`" in " ".join(read("docs/github-app.md").split()).lower()
    repos = " ".join(read("docs/architecture-repos.md").split())
    assert "Only when the App exists, the file carries `[direction] repo`" in repos


def test_board_link_click_is_only_for_a_missing_link():
    guide = " ".join(read("docs/quickstart.md").split())
    assert "already links the board" in guide
    assert "only if the Projects tab does not list it" in guide


def test_package_check_output_is_documented_as_printed():
    printed = subprocess.run(
        [sys.executable, "ops/package_skill.py", "--check", "skills/dist/agent-factory.zip"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert (printed.stdout.strip(), printed.returncode) == ("current", 0)
    doc = read("docs/chat-operator.md")
    assert "stale: installed X, source Y" in doc and "stale (installed" not in doc
    assert "exits 0 only for `current`" in " ".join(doc.split())


def test_mode_detection_uses_the_same_call_in_every_place():
    skill = " ".join(read("skills/agent-factory/SKILL.md").split())
    start = skill[skill.index("## Start: setup or take over") : skill.index("## Taking over")]
    assert "`list_projects`" in start and "queue_list" not in start
    reference = " ".join(read("skills/agent-factory/references/setup.md").split())
    assert "`list_projects`" in reference[: reference.index("## Establish where to resume")]


def test_connector_prompts_do_not_put_the_key_on_a_command_line_and_names_agree():
    guide = " ".join(read("docs/quickstart.md").split())
    assert "hidden-prompt block" in guide and "<your chat API key>" not in guide
    for path in ("docs/quickstart.md", "docs/chat-operator.md", "skills/agent-factory/references/setup.md"):
        text = " ".join(read(path).split())
        assert "claude mcp list" in text and "agent-factory" in text, path


def test_secret_storage_wording_names_the_sdk_not_a_cli_flag_for_the_bootstrap():
    guide = " ".join(read("docs/quickstart.md").split())
    assert "stored with `flyte create secret --from-file`" not in guide
    assert "through the Flyte SDK" in guide


def test_workspace_template_repository_is_not_described_as_existing():
    for path in ("docs/architecture-repos.md", "docs/standards.md"):
        text = " ".join(read(path).split())
        assert "not published" in text, path


def test_profiles_readme_points_guided_setup_users_at_the_root_profile():
    text = " ".join(read("profiles/README.md").split())
    assert "`profile.toml` at the root of your workspace" in text
    assert "ops/bootstrap.py" in text


def test_workspace_template_readme_covers_app_board_and_bootstrap():
    text = " ".join(read("src/flyte_agent_factory/_assets/workspace_template/README.md").split())
    for needle in ("GitHub App", "Projects board", "Default repository", "ops/bootstrap.py"):
        assert needle in text, needle
    assert "Replace `OWNER`" not in text or "the bootstrap sets" in text


def test_every_doc_that_gives_the_redeploy_command_gives_the_one_that_runs_the_bootstrap():
    # The bootstrap's own flag, with the exact download prefix of the first command (fail-closed), and the
    # first-run command itself must not carry the flag.
    assert "--redeploy" not in FIRST_COMMAND
    for path in SETUP_FILES:
        assert REDEPLOY in read(path), path


def test_the_setup_doc_does_not_promise_that_engine_merges_deploy_themselves_for_a_public_engine_user():
    text = " ".join(read("docs/setup.md").split())
    assert "every merge to the engine repository deploys itself" not in text
    assert "a merge ships on the next tick plus build time." not in text.replace(
        "tick plus build time. For", "X"
    )
    assert APP_ERROR not in text and all(words in text for words in ANONYMOUS) and "--redeploy" in text


def test_what_the_docs_say_about_an_uninstalled_deploy_repo_is_what_the_deployer_does():
    source = " ".join(read("src/flyte_agent_factory/_deployer.py").split())
    # The deployer falls back to anonymous reads where the App is not installed, instead of failing.
    assert "return _github.PublicGithub()" in source
    for path in (*SETUP_FILES, "docs/setup.md"):
        text = " ".join(read(path).split())
        assert "App not installed on" not in text, path
        assert APP_ERROR not in text and all(words in text for words in ANONYMOUS), path


def test_the_skill_reference_does_not_call_a_reordered_list_the_owners_order():
    text = read("skills/agent-factory/references/setup.md")
    assert (
        "in the owner's order\n" not in text
        and "## What the bootstrap sets up, in the command's order" in text
    )
    section = text.split("## What the bootstrap sets up, in the command's order", 1)[1].split("\n## ", 1)[0]
    for exception in ("`signin`", "`project`", "`ruleset`", "`engine-token`", "Flyte secrets", "factory-URL"):
        assert exception in section, f"the forced reordering is not explained: {exception}"


def test_the_template_readme_and_architecture_docs_say_main_not_a_pinned_release():
    for path in (
        "src/flyte_agent_factory/_assets/workspace_template/README.md",
        "docs/architecture-repos.md",
        "docs/architecture.md",
    ):
        assert "pinned release" not in read(path), path
    assert "public `unionai-oss/flyte-agent-factory`" in read(
        "src/flyte_agent_factory/_assets/workspace_template/README.md"
    )


def test_the_chat_verifies_the_ruleset_through_its_connector_or_the_status_line():
    for path in ("skills/agent-factory/references/setup.md", "docs/quickstart.md"):
        text = " ".join(read(path).split())
        assert re.search(r"GitHub connector[^.]{0,60}read (the workspace's )?rulesets", text), path
        assert "the `ruleset` line of" in text and "`--status`" in text, path


def test_board_click_count_is_the_same_in_code_and_docs():
    from flyte_agent_factory._bootstrap_steps import BOARD_CLICKS

    assert BOARD_CLICKS in read("src/flyte_agent_factory/_bootstrap_steps.py")
    count = BOARD_CLICKS.split()[0]
    quickstart = read("docs/quickstart.md")
    assert f'"{BOARD_CLICKS}"' in quickstart and f"these {count} clicks" in quickstart
    assert f"the {count} clicks above" in quickstart and "two board settings" not in quickstart
    assert f'"{BOARD_CLICKS}"' in read("skills/agent-factory/references/setup.md")
    template = read("src/flyte_agent_factory/_assets/workspace_template/README.md")
    assert f"so {BOARD_CLICKS}:" in " ".join(template.split()) and "**Workflows**" in template


def test_setup_reference_does_not_ask_for_the_endpoint_twice():
    reference = read("skills/agent-factory/references/setup.md")
    assert "the backend endpoint is that URL's host" in reference
    assert "the backend endpoint, project" not in reference
