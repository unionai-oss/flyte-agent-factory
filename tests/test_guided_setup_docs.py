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
    assert "--setup" in guide and "--profile mine" in guide
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
    names += ["docs/setup.md"]
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


def test_non_echo_prompts_run_in_a_bash_child_or_avoid_bash_only_flags():
    for path in guided_markdown():
        for block in SHELL_FENCE.findall(read(path)):
            if block.startswith("bash -c '"):
                continue
            assert not re.search(r"\bread\b[^\n]*\s-\w*[sp]\b", block), (
                f"{path}: zsh rejects bash's read -s/-p"
            )


SKILL_STEPS = [
    ("**GitHub.**", "## 1. GitHub access"),
    ("**Local tools and engine.**", "Clone the public engine"),
    ("**Backend.**", "uv run flyte create config"),
    ("**Profile.**", "## 3. Profile and secret names"),
    ("**Credentials.**", "## 4. Store engine credentials"),
    ("**Deploy.**", "## 5. Deploy and connect"),
    ("**Factory MCP.**", "Create or reuse a scoped **Union** API key"),
    ("**Skill and GitHub connector.**", "Package and install the deployment-specific"),
    ("**Launch.**", "## 6. Launch and confirm"),
]


def test_setup_skill_steps_follow_the_quickstart_order():
    skill = read("skills/agent-factory/references/setup.md")
    quickstart = read("docs/quickstart.md")
    steps = re.findall(r"^\d+\. (\*\*[^*]+\*\*)", skill, re.MULTILINE)
    assert steps == [title for title, _ in SKILL_STEPS]
    positions = [quickstart.index(anchor) for _, anchor in SKILL_STEPS]
    assert positions == sorted(positions)
    # The skill's one packaging and token steps sit where the quickstart puts them.
    body = {
        title: text
        for title, text in re.findall(
            r"^\d+\. (\*\*[^*]+\*\*)(.*?)(?=^\d+\. \*\*|^## )", skill, re.MULTILINE | re.DOTALL
        )
    }
    assert "package_skill.py --setup" in body["**Local tools and engine.**"]
    assert "claude setup-token" in body["**Credentials.**"]
    assert "claude setup-token" not in body["**Profile.**"]
    for title, text in body.items():
        if "claude setup-token" in text:
            assert title == "**Credentials.**"


def test_resume_does_not_overwrite_a_profile_or_repeat_an_mcp_registration():
    quickstart = read("docs/quickstart.md")
    chat = read("docs/chat-operator.md")
    assert "cp profiles/example-claude-personal.toml profiles/mine.toml" not in re.sub(
        r"\[ -e profiles/mine.toml \] \|\| cp [^\n]*", "", quickstart
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
        for heading, required in (
            ("GitHub", ("CODEOWNERS", "@OWNER")),
            ("Profile", ("filled-in", "[state]", "[backend]", "[repo]", "[secrets]", "never overwrite")),
            ("Deploy", ("[mcp] public_url", "only", "one copy-paste command")),
            ("Factory MCP", ("grant block", "client_id", "factory-chat", "one copy-paste command")),
        ):
            if path.startswith("skills/"):
                found = re.search(rf"\*\*{heading}\.\*\*(.*?)(?=^\d+\. |^## |\Z)", text, re.S | re.M)
                assert found, f"{path}: no **{heading}.** step"
                section = found[1]
            else:
                anchors = {
                    "GitHub": ("## 1.", "## 2."),
                    "Profile": ("## 3.", "## 4."),
                    "Deploy": ("## 5.", "Follow [chat-operator.md"),
                    "Factory MCP": ("Follow [chat-operator.md", "## 6."),
                }
                start, end = anchors[heading]
                section = text.split(start, 1)[1].split(end, 1)[0]
            for value in required:
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
