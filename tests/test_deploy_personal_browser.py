"""A profile with the personal browser deploys from an interpreter without `kubernetes`.

The deployer and the slot pods run in the agent image, which has no `kubernetes` package. Turning on
`[browser] personal` once made `browser.py` import it at module level, and every slot deploy failed on
`ModuleNotFoundError`. These tests run the deploy check in a real interpreter that cannot import it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import flyte
import flyte.app
import pytest

from flyte_agent_factory import _deploy, _profile

SRC = Path(__file__).resolve().parents[1] / "src"
WORK = (Path(__file__).parent / "profiles/work.toml").read_text()
PERSONAL = (
    '\n[browser]\npersonal = true\npersonal_env = "agent-one"\nexit_node = "home-router"\n'
    'profile_uri = "memory://profile"\n'
)
TWO = (
    '\n[browser]\nexit_node = "home-router"\ntailscale_tag = "tag:factory-browser"\n'
    '[browser.identities.github]\nprofile_uri = "memory://github"\n'
    '[browser.identities.linkedin]\nprofile_uri = "memory://linkedin"\nexit_node = ""\n'
)


@pytest.fixture(scope="module")
def no_kubernetes(tmp_path_factory) -> str:
    """An interpreter with this suite's packages except `kubernetes`, as the agent image is.

    A venv whose `.pth` adds this environment's site-packages and then makes `import kubernetes` fail.
    It is the venv's own `sys.executable`, so every subprocess the deploy check starts is one too.
    """
    venv = tmp_path_factory.mktemp("no-kubernetes")
    subprocess.run(
        [getattr(sys, "_base_executable", sys.executable), "-m", "venv", "--without-pip", str(venv)],
        check=True,
    )
    python = venv / "bin" / "python"
    purelib = subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True
    ).strip()
    Path(purelib, "zz_no_kubernetes.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\nimport sys; sys.modules['kubernetes'] = None\n"
    )
    probe = subprocess.run([str(python), "-c", "import kubernetes"], capture_output=True, text=True)
    assert "ModuleNotFoundError" in probe.stderr
    subprocess.run([str(python), "-c", "import flyte"], check=True)
    return str(python)


def _env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in (_profile.PROFILE_ENV, _profile.TOML_ENV, "VIRTUAL_ENV", "PYTHONPATH")
        and not k.startswith(("AGENT_FACTORY_", "BROWSER_"))
    }
    return {**env, "PYTHONPATH": str(SRC), **extra}


def _check(python: str, text: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    error = tmp_path / "error"
    return subprocess.run(
        [python, "-m", "flyte_agent_factory._deploy_child", "check", "--src", str(SRC), "--where", "t"]
        + ["--error", str(error)],
        input=text,
        env=_env(),
        cwd=SRC,
        capture_output=True,
        text=True,
        timeout=_deploy.CHECK_TIMEOUT,
        check=False,
    )


def _imported(stderr: str) -> set[str]:
    lines = [line for line in stderr.splitlines() if line.startswith("imported: ")]
    return {name for line in lines for name in json.loads(line.removeprefix("imported: "))}


@pytest.mark.parametrize("browser", [PERSONAL, ""], ids=["personal", "no-personal"])
def test_deploy_check_imports_every_environment_without_kubernetes(no_kubernetes, tmp_path, browser):
    profile = _profile.parse(WORK + browser)
    result = _check(no_kubernetes, WORK + browser, tmp_path)
    assert result.returncode == 0, result.stderr
    planned = json.loads(result.stdout.strip().splitlines()[-1])
    assert planned == _deploy.planned_envs(profile)
    # Not only planned: every environment the deploy children would deploy was imported, under its recipe.
    assert _imported(result.stderr) == set(planned)


def test_only_the_personal_env_slot_gets_the_personal_browser(no_kubernetes):
    """Name kept from before #90, which reversed the rule: `personal_env` no longer singles out a slot.

    Every slot now mounts every identity's token (for its supervisor only), and which project may use a
    browser is decided per run by its grant (tests/test_browser_gate.py, tests/test_factory_browser.py).
    """
    profile = _profile.parse(WORK + PERSONAL)
    code = (
        "import json; from flyte_agent_factory import agent, browser; "
        "print(json.dumps({'env': agent.env.env_vars, 'secrets': {s.key: s.as_env_var "
        "for s in agent.env.secrets}, 'apps': sorted(a.name for a in browser.identity_apps.values())}))"
    )
    seen = {}
    for slot in profile.slots:
        env = _profile.child_env(profile, _deploy.recipe_env(profile, slot.env, _env()))
        out = subprocess.run(
            [no_kubernetes, "-c", code], env=env, capture_output=True, text=True, check=False
        )
        assert out.returncode == 0, out.stderr
        seen[slot.env] = json.loads(out.stdout)
    assert set(seen) == {"agent-one", "agent-two"}
    for definition in seen.values():
        assert definition["apps"] == ["claude-browser-personal"]
        assert definition["secrets"]["browser_personal_token"] == "BROWSER_PERSONAL_TOKEN"
        assert "BROWSER_PERSONAL_ENABLED" not in definition["env"]  # the grant is per run now


def test_every_slot_mounts_all_identity_tokens_and_knows_their_names(no_kubernetes):
    profile = _profile.parse(WORK + TWO)
    code = (
        "import json; from flyte_agent_factory import agent; "
        "print(json.dumps({'env': agent.env.env_vars, 'secrets': {s.key: s.as_env_var "
        "for s in agent.env.secrets}}))"
    )
    for slot in profile.slots:
        env = _profile.child_env(profile, _deploy.recipe_env(profile, slot.env, _env()))
        out = subprocess.run(
            [no_kubernetes, "-c", code], env=env, capture_output=True, text=True, check=False
        )
        assert out.returncode == 0, out.stderr
        definition = json.loads(out.stdout)
        assert definition["secrets"]["browser_github_token"] == "BROWSER_GITHUB_TOKEN"
        assert definition["secrets"]["browser_linkedin_token"] == "BROWSER_LINKEDIN_TOKEN"
        assert "browser_personal_token" not in definition["secrets"]
        assert definition["env"]["AGENT_FACTORY_BROWSER_IDENTITY_NAMES"] == "github,linkedin"
        assert "BROWSER_PERSONAL_ENABLED" not in definition["env"]  # the grant is per run now


def test_a_profile_without_identities_mounts_no_browser_token(no_kubernetes):
    profile = _profile.parse(WORK)
    code = (
        "import json; from flyte_agent_factory import agent, browser; "
        "print(json.dumps({'env': agent.env.env_vars, 'secrets': [s.key for s in agent.env.secrets], "
        "'apps': list(browser.identity_apps), 'legacy': browser.personal_browser_env is None}))"
    )
    env = _profile.child_env(profile, _deploy.recipe_env(profile, profile.slots[0].env, _env()))
    out = subprocess.run([no_kubernetes, "-c", code], env=env, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    definition = json.loads(out.stdout)
    assert not [k for k in definition["secrets"] if k.startswith("browser_")]
    assert definition["apps"] == [] and definition["legacy"]
    assert definition["env"]["AGENT_FACTORY_BROWSER_IDENTITY_NAMES"] == ""
    assert "BROWSER_PERSONAL_ENABLED" not in definition["env"]


def _env_vars(app: flyte.app.AppEnvironment) -> dict[str, str]:
    assert app.env_vars is not None
    return dict(app.env_vars)


def _secrets(app: flyte.app.AppEnvironment) -> list[flyte.Secret]:
    """The app's secrets; a plain string would mean a secret the test did not expect."""
    secrets = [app.secrets] if isinstance(app.secrets, (str, flyte.Secret)) else app.secrets or []
    assert all(isinstance(s, flyte.Secret) for s in secrets)
    return [s for s in secrets if isinstance(s, flyte.Secret)]


def test_each_identity_is_its_own_app_with_its_own_token_and_archive(monkeypatch):
    from flyte_agent_factory import browser

    monkeypatch.setattr(browser, "_EXIT_NODE", "home-router")  # what the TWO profile's [browser] says
    profile = _profile.parse(WORK + TWO)
    github, linkedin = profile.browser.identities
    apps = {
        i.name: browser.personal_app(profile, i, browser.identity_exit_node(i)) for i in (github, linkedin)
    }
    assert [a.name for a in apps.values()] == ["claude-browser-github", "claude-browser-linkedin"]
    assert _env_vars(apps["github"])["BROWSER_PROFILE_URI"] == "memory://github"
    assert _env_vars(apps["linkedin"])["BROWSER_PROFILE_URI"] == "memory://linkedin"
    assert _env_vars(apps["github"])["BROWSER_IDENTITY"] == "github"
    assert _env_vars(apps["github"])["BROWSER_TAILSCALE_TAG"] == "tag:factory-browser"
    # The app reads its own token under one name whatever the identity is called.
    assert [(s.key, s.as_env_var) for s in _secrets(apps["github"])][0] == (
        "browser_github_token",
        "BROWSER_PERSONAL_TOKEN",
    )
    assert [s.key for s in _secrets(apps["github"])] == [
        "browser_github_token",
        "tailscale_oauth_client_id",
        "tailscale_oauth_client_secret",
    ]
    # linkedin overrides the default exit node with "" (direct egress): no exit node, no tailscale pair.
    assert _env_vars(apps["linkedin"])["BROWSER_EXIT_NODE"] == ""
    assert [s.key for s in _secrets(apps["linkedin"])] == ["browser_linkedin_token"]


def test_the_deploy_override_of_the_exit_node_applies_to_inheriting_identities_only(monkeypatch):
    from flyte_agent_factory import browser

    monkeypatch.setattr(browser, "_EXIT_NODE", "override-node")
    profile = _profile.parse(WORK + TWO)
    assert [browser.identity_exit_node(i) for i in profile.browser.identities] == ["override-node", ""]


def test_the_legacy_profile_yields_exactly_the_old_app(monkeypatch):
    from flyte_agent_factory import browser

    profile = _profile.parse(WORK + PERSONAL)
    (identity,) = profile.browser.identities
    app = browser.personal_app(profile, identity, "home-router")
    assert app.name == "claude-browser-personal"
    assert _env_vars(app)["BROWSER_PROFILE_URI"] == "memory://profile"
    assert _env_vars(app)["BROWSER_EXIT_NODE"] == "home-router"
    assert _env_vars(app)["BROWSER_TAILSCALE_TAG"] == "tag:k8s"
    assert [(s.key, s.as_env_var) for s in _secrets(app)] == [
        ("browser_personal_token", "BROWSER_PERSONAL_TOKEN"),
        ("tailscale_oauth_client_id", "TAILSCALE_OAUTH_CLIENT_ID"),
        ("tailscale_oauth_client_secret", "TAILSCALE_OAUTH_CLIENT_SECRET"),
    ]


def test_identity_endpoints(monkeypatch, tmp_path):
    from flyte_agent_factory import browser

    profile = _profile.parse(WORK + TWO)
    apps = {i.name: browser.personal_app(profile, i, "") for i in profile.browser.identities}
    monkeypatch.setattr(browser, "identity_apps", apps)
    namespace = tmp_path / "namespace"
    namespace.write_text("ns-1\n")
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", str(namespace))
    assert browser.identity_endpoint("github") == "http://claude-browser-github.ns-1.svc.cluster.local:80"
    with pytest.raises(RuntimeError, match="no browser identity 'nobody'"):
        browser.identity_endpoint("nobody")
    with pytest.raises(RuntimeError, match="no browser identity 'nobody'"):
        browser.identity_public_endpoint("nobody")
    # Outside a pod the deployed app's endpoint, and its public URL, are used.
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(
        type(apps["github"]), "endpoint", property(lambda self: f"https://apps.example/{self.name}")
    )
    assert browser.identity_endpoint("github") == "https://apps.example/claude-browser-github"
    assert browser.identity_public_endpoint("linkedin") == "https://apps.example/claude-browser-linkedin"
    # The single-browser callers need one unambiguous identity.
    with pytest.raises(RuntimeError, match="not enabled"):
        browser.personal_endpoint()
    monkeypatch.setattr(browser, "identity_apps", {"github": apps["github"]})
    assert browser.personal_endpoint() == "https://apps.example/claude-browser-github"


def test_the_check_refuses_a_profile_whose_environments_do_not_import(monkeypatch, tmp_path):
    import io

    from flyte_agent_factory import _deploy_child

    monkeypatch.setattr(_deploy_child, "_IMPORT_SLOT", "raise SystemExit('cannot define the slot')")
    monkeypatch.setattr(sys, "stdin", io.StringIO(WORK + PERSONAL))
    error = tmp_path / "error"
    argv = ["check", "--src", str(SRC), "--where", "t", "--error", str(error)]
    assert _deploy_child.main(argv) == 1
    assert error.read_text() == "importing agent-one failed: cannot define the slot"


def test_the_personal_app_serializes_with_its_grace_period():
    # Serializing needs kubernetes, as deploying the app does; importing the module does not (above).
    import flyte
    from flyte.app._runtime.app_serde import _get_k8s_pod
    from flyte.models import SerializationContext
    from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

    from flyte_agent_factory import browser

    profile = _profile.parse(WORK + PERSONAL)
    app = browser.personal_app(profile, profile.browser.identities[0], "home-router")
    assert app.name == "claude-browser-personal"
    assert isinstance(app.pod_template, flyte.PodTemplate)
    pod = MessageToDict(_get_k8s_pod(app, app.pod_template, SerializationContext(version="v1")).pod_spec)
    assert pod["terminationGracePeriodSeconds"] == 90
    assert [c["name"] for c in pod["containers"]] == ["app"]
    assert pod["containers"][0]["command"] == ["/bin/bash", "/opt/browser/start.sh"]


def test_view_link_for_initialises_flyte_in_a_fresh_process(monkeypatch):
    import contextlib
    import io
    import urllib.request

    import flyte
    from flyte.errors import InitializationError

    from flyte_agent_factory import browser

    initialised: list[bool] = []

    class App:
        @property
        def endpoint(self):
            if not initialised:
                raise InitializationError(
                    "ClientNotInitializedError", "user", "Client has not been initialized."
                )
            return "https://apps.example/personal/"

    monkeypatch.setattr(flyte, "init_in_cluster", lambda: initialised.append(True))
    monkeypatch.setitem(browser.identity_apps, "personal", App())
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: "http://private-browser")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: contextlib.nullcontext(io.BytesIO(b"t-1"))
    )
    assert browser.view_link_for("personal") == "https://apps.example/personal/view-login?ticket=t-1"
    assert initialised == [True]


def test_view_link_initialises_flyte_in_a_fresh_slot_process(monkeypatch):
    """The factory pod's `view_link_for` (a fresh process) initialises Flyte to find the public URL."""
    import contextlib
    import io
    import urllib.request

    import flyte
    from flyte.errors import InitializationError

    from flyte_agent_factory import browser

    initialised: list[bool] = []

    class App:
        name = "claude-browser-personal"

        @property
        def endpoint(self) -> str:
            if not initialised:
                raise InitializationError(
                    "ClientNotInitializedError", "user", "Client has not been initialized."
                )
            return "https://apps.example/personal/"

    monkeypatch.setattr(flyte, "init_in_cluster", lambda: initialised.append(True))
    monkeypatch.setattr(browser, "identity_apps", {"personal": App()})
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: "http://private-browser")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: contextlib.nullcontext(io.BytesIO(b"t-1"))
    )
    assert browser.view_link_for("personal") == "https://apps.example/personal/view-login?ticket=t-1"
    assert initialised == [True]


# --- the apps stage ----------------------------------------------------------------------------


def test_the_personal_browser_is_planned_only_when_the_profile_enables_it():
    on, off = _profile.parse(WORK + PERSONAL), _profile.parse(WORK)
    assert _deploy.app_envs(on) == {"claude-browser-personal"} and not _deploy.app_envs(off)
    assert "claude-browser-personal" in _deploy.planned_envs(on)
    assert "claude-browser-personal" not in _deploy.planned_envs(off)


def test_every_identity_is_planned_as_its_own_app():
    profile = _profile.parse(WORK + TWO)
    assert _deploy.app_envs(profile) == {"claude-browser-github", "claude-browser-linkedin"}
    assert {"claude-browser-github", "claude-browser-linkedin"} <= set(_deploy.planned_envs(profile))
    assert "claude-browser-personal" not in _deploy.planned_envs(profile)


def test_the_shared_browser_name_is_the_one_the_deployer_never_retires():
    from flyte_agent_factory import browser

    assert _deploy.SHARED_BROWSER_APP == browser.APP_NAME


def test_a_deploy_runs_slots_then_the_factory_then_the_apps(monkeypatch, tmp_path):
    from flyte_agent_factory import _deploy_child

    order: list[str] = []

    def stage_of(name):
        def stage(*args, **kwargs):
            order.append(name)
            return 0

        return stage

    for stage in ("deploy_slots", "deploy_factory", "deploy_apps"):
        monkeypatch.setattr(_deploy, stage, stage_of(stage))
    profile = _profile.parse(WORK + PERSONAL)
    _deploy_child.deploy(profile, tmp_path, project="p", domain="d", ref="x", base_env={})
    assert order == ["deploy_slots", "deploy_factory", "deploy_apps"]


@pytest.mark.parametrize(
    ("failing", "label"),
    [("deploy_slots", "slot"), ("deploy_factory", "factory"), ("deploy_apps", "apps")],
)
def test_a_failed_stage_carries_the_end_of_its_stderr_redacted(monkeypatch, tmp_path, failing, label):
    from flyte_agent_factory import _deploy_child

    def stage(*args, stderr_tail, **kw):
        stderr_tail.extend(["noise", "claude-browser-personal failed: clone of https://u:hunter2@x.org/r"])
        return 1

    for name in ("deploy_slots", "deploy_factory", "deploy_apps"):
        monkeypatch.setattr(_deploy, name, stage if name == failing else lambda *a, **kw: 0)
    profile = _profile.parse(WORK + PERSONAL)
    with pytest.raises(_deploy.DeployError) as caught:
        _deploy_child.deploy(profile, tmp_path, project="p", domain="d", ref="x", base_env={})
    message = str(caught.value)
    assert message.startswith(f"{label} deploy of x failed: noise | claude-browser-personal failed:")
    assert "hunter2" not in message and "https://u:***@x.org" in message


def test_a_child_streams_its_stderr_and_keeps_the_last_lines(tmp_path, capsys):
    profile = _profile.parse(WORK)
    script = "import sys\nfor i in range(30): print(f'line {i}', file=sys.stderr)\nsys.exit(2)"
    tail: list[str] = []
    code = _deploy._run_child(profile, script, tmp_path, {}, dry_run=False, stderr_tail=tail)
    assert code == 2
    assert tail == [f"line {i}" for i in range(10, 30)]
    assert "line 0\n" in capsys.readouterr().err  # all of it went through, not just the tail


def test_the_apps_child_without_kubernetes_says_so_instead_of_a_traceback(no_kubernetes, tmp_path):
    profile = _profile.parse(WORK + PERSONAL)
    env = _profile.child_env(profile, _env(_UNION_EAGER_API_KEY="unused"))
    done = subprocess.run(
        [no_kubernetes, "-c", _deploy.APPS_CHILD, str(SRC), "0", "p", "d"],
        env=env,
        cwd=SRC,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "lacks the kubernetes package" in done.stderr and "will retry" in done.stderr
    assert "Traceback" not in done.stderr


def test_the_apps_child_records_what_it_deployed():
    # The child's last step is `record(deploy_app(env))`: an app that was not active is not recorded.
    assert "record(_deploy_apps.deploy_app(env, dry_run=dry))" in _deploy.APPS_CHILD
    assert _deploy.APPS_CHILD.index("import kubernetes") < _deploy.APPS_CHILD.index("init_from_api_key")


def test_the_apps_stage_runs_when_the_profile_enables_no_app(monkeypatch):
    # It must still run: an app deployed for an earlier profile is stopped there.
    ran = []

    def child(profile, script, *args, **kwargs):
        ran.append(script)
        return 0

    monkeypatch.setattr(_deploy, "_run_child", child)
    assert _deploy.deploy_apps(_profile.parse(WORK), SRC, project="p", domain="d", env={}) == 0
    assert ran == [_deploy.APPS_CHILD]


def test_the_apps_child_stops_dropped_apps_before_deploying_and_without_kubernetes():
    child = _deploy.APPS_CHILD
    assert "retire_apps(APPS - enabled" in child
    assert child.index("retire_apps(") < child.index("deploy_app(")
    assert "deployed_personal_apps() - APPS - enabled" in child  # identities dropped from the profile
    # Stopping needs neither kubernetes nor browser.py: the guard applies only when an app is enabled.
    assert child.index("if enabled:") < child.index("import kubernetes")
    assert child.index("retire_apps(") < child.index("from flyte_agent_factory import browser")


def test_every_app_a_profile_can_enable_is_known():
    assert _deploy.app_envs(_profile.parse(WORK + PERSONAL)) <= _deploy.APPS
    assert _deploy.app_envs(_profile.parse(WORK)) == frozenset()


# --- secrets are checked before anything is deployed -------------------------------------------


def _main_check(monkeypatch, tmp_path, text):
    import io

    from flyte_agent_factory import _deploy_child

    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    error = tmp_path / "error"
    argv = ["check", "--src", str(SRC), "--where", "t", "--error", str(error)]
    return _deploy_child.main(argv), error


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "unused")


def test_a_missing_secret_stops_the_check_naming_it_and_what_needs_it(monkeypatch, tmp_path, capsys, key):
    from flyte_agent_factory import _deploy_child

    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", lambda project, domain: set())
    code, error = _main_check(monkeypatch, tmp_path, WORK + PERSONAL)
    assert code == 3
    out = capsys.readouterr().out.strip().splitlines()
    missing = json.loads(out[-1])["missing_secrets"]
    assert "claude-browser-personal" in missing["browser_personal_token"]
    assert "claude-browser-personal" in missing["tailscale_oauth_client_id"]
    message = error.read_text()
    assert message.startswith("missing Flyte secrets in ")
    assert "browser_personal_token (needed by " in message and "claude-browser-personal" in message
    assert "`flyte create secret <name>`" in message and "nothing was deployed" in message
    assert out[-1].startswith('{"missing_secrets"')  # and no environment list: the check did not pass


def test_the_check_names_each_identitys_missing_token(monkeypatch, tmp_path, capsys, key):
    from flyte_agent_factory import _deploy_child

    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", lambda project, domain: set())
    code, error = _main_check(monkeypatch, tmp_path, WORK + TWO)
    assert code == 3
    missing = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["missing_secrets"]
    assert "claude-browser-github" in missing["browser_github_token"]
    assert "claude-browser-linkedin" in missing["browser_linkedin_token"]
    assert "claude-browser-linkedin" not in missing["tailscale_oauth_client_id"]  # direct egress
    message = error.read_text()
    assert "browser_github_token (needed by " in message and "claude-browser-github" in message
    assert "browser_linkedin_token (needed by " in message and "claude-browser-linkedin" in message


def test_the_check_defines_every_identity_app(key):
    from flyte_agent_factory import _deploy_child

    profile = _profile.parse(WORK + TWO)
    needed = _deploy_child.collect_planned_secrets(profile, SRC, _env())
    assert needed["claude-browser-github"][0] == "browser_github_token"
    assert needed["claude-browser-linkedin"] == ["browser_linkedin_token"]


def test_the_check_passes_when_every_secret_exists(monkeypatch, tmp_path, capsys, key):
    from flyte_agent_factory import _deploy_child

    profile = _profile.parse(WORK + PERSONAL)
    needed = _deploy_child.collect_planned_secrets(profile, SRC, _env())
    assert "browser_personal_token" in needed["claude-browser-personal"]
    everything = {secret for secrets in needed.values() for secret in secrets}
    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", lambda project, domain: everything)
    code, _ = _main_check(monkeypatch, tmp_path, WORK + PERSONAL)
    assert code == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == _deploy.planned_envs(profile)


def test_secrets_that_cannot_be_listed_do_not_block_the_deploy(monkeypatch, tmp_path, capsys, key):
    from flyte_agent_factory import _deploy_child

    def listing_fails(project, domain):
        raise ConnectionError("https://u:hunter2@x.org down")

    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", listing_fails)
    code, _ = _main_check(monkeypatch, tmp_path, WORK + PERSONAL)
    err = capsys.readouterr().err
    assert code == 0
    assert "secrets not verified" in err and "ConnectionError" in err and "hunter2" not in err


def test_no_api_key_does_not_block_the_deploy(monkeypatch, tmp_path, capsys):
    from flyte_agent_factory import _deploy_child

    monkeypatch.delenv("_UNION_EAGER_API_KEY", raising=False)
    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", lambda *a: pytest.fail("no key, no lookup"))
    code, _ = _main_check(monkeypatch, tmp_path, WORK + PERSONAL)
    assert code == 0 and "secrets not verified: no API key" in capsys.readouterr().err


def test_the_secret_lookup_uses_the_deploy_project_and_domain(monkeypatch, tmp_path, key):
    from flyte_agent_factory import _deploy_child

    seen: list[tuple[str, str]] = []

    def existing(project, domain):
        seen.append((project, domain))
        return set()

    monkeypatch.setenv("FLYTE_MCP_PROJECT", "proj-x")
    monkeypatch.setenv("FLYTE_MCP_DOMAIN", "dom-y")
    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", existing)
    code, error = _main_check(monkeypatch, tmp_path, WORK + PERSONAL)
    assert seen == [("proj-x", "dom-y")] and code == 3
    assert "missing Flyte secrets in proj-x/dom-y: " in error.read_text()


def test_the_tree_check_raises_missing_secrets_on_exit_code_3(monkeypatch, tmp_path):
    def run_in_tree(src, args, **kw):
        Path(args[args.index("--error") + 1]).write_text("missing Flyte secrets in p/d: s (needed by e)")
        return 3, '{"missing_secrets": {"s": ["e"]}}\n', ""

    monkeypatch.setattr(_deploy, "_run_in_tree", run_in_tree)
    with pytest.raises(_deploy.MissingSecrets, match="missing Flyte secrets in p/d: s") as caught:
        _deploy.check_in_tree(tmp_path, "", "t", tree=None, base_env={})
    assert caught.value.secrets == {"s": ["e"]}
    assert isinstance(caught.value, _deploy.DeployError)


def test_the_doc_says_a_dropped_browser_is_stopped_not_deleted():
    doc = " ".join((SRC.parent / "docs" / "personal-browser.md").read_text().split())
    assert "stops the app on the deployer's next tick, whatever state it is in" in doc
    assert "deactivated, not deleted" in doc


def test_a_check_that_finds_every_secret_says_so(monkeypatch, capsys):
    from flyte_agent_factory import _deploy_child

    monkeypatch.setattr(_deploy_child, "_list_existing_secrets", lambda p, d: {"a", "b", "c"})
    needed = {"env-1": ["a", "b"], "env-2": ["b"]}
    assert _deploy_child.find_missing_secrets(needed, "p", "d", {"_UNION_EAGER_API_KEY": "k"}) == {}
    assert "secrets verified: all 2 present in p/d: a, b" in capsys.readouterr().err


_FAILED_APP_STUB = """
flyte.init_from_api_key = lambda **kw: None
from flyteidl2.app import app_definition_pb2 as _pb
import flyte.remote as _remote
from flyte.remote import _app as _sdk


class _FailedApp:
    deployment_status = _pb.Status.DeploymentStatus.DEPLOYMENT_STATUS_FAILED

    def is_deactivated(self):
        return _sdk._is_deactivated(self.deployment_status)

    def deactivate(self, wait=False):
        print(f"DEACTIVATE wait={wait}")


class _Apps:
    @staticmethod
    def get(name):
        return _FailedApp()

    @staticmethod
    def listall(limit=100):
        return []


_remote.App = _Apps
"""


def test_the_real_apps_child_stops_a_failed_browser_the_profile_dropped(tmp_path):
    # End to end: the apps child itself, a profile without the browser, and a crash-looping app on the server.
    profile = _profile.parse(WORK)
    script = _deploy.APPS_CHILD.replace(
        "import os, sys, flyte\n", "import os, sys, flyte\n" + _FAILED_APP_STUB, 1
    )
    env = _profile.child_env(profile, _env(_UNION_EAGER_API_KEY="unused"))
    done = subprocess.run(
        [sys.executable, "-c", script, str(SRC), "0", "p", "d"],
        env=env,
        cwd=SRC,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "DEACTIVATE wait=False" in done.stdout
    assert "claude-browser-personal: stop requested, the profile does not enable it" in done.stdout


def test_the_factory_mounts_every_identitys_token_for_browser_link():
    from flyte_agent_factory import _factory

    def mounted(text):
        return {
            s.key: s.as_env_var for s in _factory._env_secrets(_profile.parse(text)) if not isinstance(s, str)
        }

    assert mounted(WORK + TWO)["browser_github_token"] == "BROWSER_GITHUB_TOKEN"
    assert mounted(WORK + TWO)["browser_linkedin_token"] == "BROWSER_LINKEDIN_TOKEN"
    assert not [key for key in mounted(WORK) if key.startswith("browser_")]


def test_the_check_names_the_identity_tokens_for_the_factory_environment(key):
    from flyte_agent_factory import _deploy_child

    needed = _deploy_child.collect_planned_secrets(_profile.parse(WORK + TWO), SRC, _env())
    assert {"browser_github_token", "browser_linkedin_token"} <= set(needed["factory"])
