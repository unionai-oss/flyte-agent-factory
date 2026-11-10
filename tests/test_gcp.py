"""`gcp_bootstrap`: sign-in by link, then a least-privilege service-account key. No network."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from flyte_agent_factory import _gcp, _profile, _session_config

TOKEN = "ya29.OWNER-TOKEN-SECRET"
KEY_JSON = json.dumps({"type": "service_account", "private_key": "PRIVATE-KEY-MATERIAL"})
PROJECT = "my-infra-123"
BILLING = "012345-6789AB-CDEF01"
SA = f"serviceAccount:factory-infra@{PROJECT}.iam.gserviceaccount.com"


class FakeGcp:
    """Just enough of Google's APIs, with state, so a second run finds what the first left."""

    def __init__(self, *, project=False, billing="", sa=False, bindings=None, keys=(), fail=None):
        self.project = project
        self.billing = billing
        self.sa = sa
        self.project_policy: dict[str, Any] = {"etag": "e1", "bindings": list(bindings or [])}
        self.billing_policy: dict[str, Any] = {"etag": "b1", "bindings": []}
        self.keys = list(keys)
        self.labels = {}
        self.fail = fail or {}  # substring of "METHOD url" -> HTTP status
        self.calls: list[tuple[str, str]] = []
        self.token_requests: list[dict[str, Any]] = []
        self.revoked: list[str] = []
        self.exchange_ok = True
        self.apis_enabled: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, url = request.method, str(request.url).split("?")[0]
        self.calls.append((method, url))
        body = (
            json.loads(request.content)
            if request.content and request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        for needle, status in self.fail.items():
            if needle in f"{method} {url}":
                return self._error(status)
        if url == _gcp.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if not self.exchange_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})
            return httpx.Response(200, json={"access_token": TOKEN})
        if url == _gcp.REVOKE_URL:
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return self._route(method, url, body)

    @staticmethod
    def _error(status: int) -> httpx.Response:
        return httpx.Response(status, json={"error": {"status": "STATUS", "message": f"boom {status}"}})

    def _route(self, method: str, url: str, body: dict[str, Any]) -> httpx.Response:
        if url.endswith("/v3/projects") and method == "POST":
            if self.project:
                return self._error(409)
            self.project = True
            return httpx.Response(200, json={"name": "operations/op1", "done": True})
        if "/v3/projects/" in url and url.endswith(":getIamPolicy"):
            return httpx.Response(200, json=json.loads(json.dumps(self.project_policy)))
        if "/v3/projects/" in url and url.endswith(":setIamPolicy"):
            self.project_policy = body["policy"]
            return httpx.Response(200, json={})
        if "/v3/projects/" in url and method == "GET":
            return httpx.Response(
                200,
                json={"state": "ACTIVE" if self.project else "DELETE_REQUESTED", "labels": dict(self.labels)},
            )
        if "/v3/projects/" in url and method == "PATCH":
            self.labels = body["labels"]
            return httpx.Response(200, json={"done": True})
        if url.endswith("/billingInfo") and method == "GET":
            on = bool(self.billing)
            return httpx.Response(
                200,
                json={
                    "billingEnabled": on,
                    "billingAccountName": f"billingAccounts/{self.billing}" if on else "",
                },
            )
        if url.endswith("/billingInfo") and method == "PUT":
            self.billing = body["billingAccountName"].split("/")[1]
            return httpx.Response(200, json={})
        if url.endswith(":batchEnable"):
            self.apis_enabled += body["serviceIds"]
            return httpx.Response(200, json={"name": "operations/enable", "done": False})
        if url.endswith("operations/enable"):
            return httpx.Response(200, json={"name": "operations/enable", "done": True})
        if url.endswith("/serviceAccounts") and method == "POST":
            if self.sa:
                return self._error(409)
            self.sa = True
            return httpx.Response(200, json={})
        if "billingAccounts" in url and url.endswith(":getIamPolicy"):
            return httpx.Response(200, json=json.loads(json.dumps(self.billing_policy)))
        if "billingAccounts" in url and url.endswith(":setIamPolicy"):
            self.billing_policy = body["policy"]
            return httpx.Response(200, json={})
        if url.endswith("/keys") and method == "POST":
            name = f"projects/{PROJECT}/serviceAccounts/x/keys/new"
            self.keys.append(name)
            data = base64.b64encode(KEY_JSON.encode()).decode()
            return httpx.Response(200, json={"name": name, "privateKeyData": data})
        if url.endswith("/keys") and method == "GET":
            return httpx.Response(200, json={"keys": [{"name": k} for k in self.keys]})
        if "/keys/" in url and method == "DELETE":
            self.keys.remove(url.split("googleapis.com/v1/")[1])
            return httpx.Response(200, json={})
        if "/regions/" in url:
            quotas = [
                {"metric": "CPUS", "limit": 24, "usage": 0},
                {"metric": _gcp.QUOTA_METRIC, "limit": 4, "usage": 1},
            ]
            return httpx.Response(200, json={"quotas": quotas})
        raise AssertionError(f"unexpected call {method} {url}")


async def no_sleep(_):
    return None


class Run:
    def __init__(self, fake: FakeGcp, codes=("the-code",), store_error=None):
        self.fake = fake
        self.codes = list(codes)
        self.prompts: list[str] = []
        self.stored: dict[str, str] = {}
        self.logs: list[str] = []
        self.store_error = store_error

    async def ask(self, prompt, attempt):
        self.prompts.append(prompt)
        return self.codes.pop(0)

    async def store(self, name, value):
        if self.store_error:
            raise self.store_error
        self.stored[name] = value

    async def go(self, **overrides):
        args = {
            "project_id": PROJECT,
            "billing_account": BILLING,
            "create_project": False,
            "region": "us-west1",
            "secret_name": "gcp_key",
            "roles": ["roles/owner"],
            "billing_roles": ["roles/billing.costsManager"],
            "apis": ["compute", "iam.googleapis.com"],
            "ask": self.ask,
            "store_secret": self.store,
            "log": lambda icon, text: self.logs.append(f"{icon} {text}"),
            "sleep": no_sleep,
            **overrides,
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.fake.handler)) as http:
            return await _gcp.bootstrap(http=http, **args)


async def test_the_link_is_a_clickable_markdown_link_with_pkce():
    run = Run(FakeGcp(project=True, billing=BILLING))
    await run.go()
    [prompt] = run.prompts
    url = prompt.split("](")[1].split(")")[0]
    assert prompt.count("- [Open the Google sign-in page](") == 1
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [_gcp.SCOPE] and query["redirect_uri"] == [_gcp.REDIRECT_URI]
    assert "oob" not in url and query["code_challenge_method"] == ["S256"]
    assert "access_type" not in query  # no refresh token is requested


async def test_the_code_is_exchanged_with_the_matching_pkce_verifier():
    run = Run(FakeGcp(project=True, billing=BILLING), codes=["  the-code \n"])
    await run.go()
    challenge = parse_qs(urlparse(run.prompts[0].split("](")[1].split(")")[0]).query)["code_challenge"][0]
    [form] = run.fake.token_requests
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    assert form["code"] == "the-code" and form["redirect_uri"] == _gcp.REDIRECT_URI


async def test_a_rejected_code_gets_a_fresh_link_and_gives_up_after_the_attempts():
    fake = FakeGcp(project=True, billing=BILLING)
    fake.exchange_ok = False
    run = Run(fake, codes=["bad1", "bad2"])
    with pytest.raises(_gcp.StepError, match="did not complete in 2 attempts"):
        await run.go(attempts=2)
    assert len(run.prompts) == 2 and run.prompts[0] != run.prompts[1]
    assert fake.revoked == []  # no token was ever issued


async def test_abort_ends_the_sign_in():
    run = Run(FakeGcp(), codes=["/abort"])
    with pytest.raises(_gcp.StepError, match="aborted"):
        await run.go()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"project_id": ""}, "project_id is required"),
        ({"project_id": "  "}, "project_id is required"),
        ({"project_id": "Bad_ID"}, "not a valid GCP project id"),
        ({"billing_account": "nope"}, "billing_account must look like"),
        ({"create_project": True, "billing_account": ""}, "needs a billing_account"),
        ({"region": "mars"}, "region"),
        ({"secret_name": ""}, "no secret name"),
    ],
)
async def test_it_refuses_before_any_sign_in(overrides, message):
    run = Run(FakeGcp())
    with pytest.raises(ValueError, match=message):
        await run.go(**overrides)
    assert run.prompts == [] and run.fake.calls == []


async def test_a_fresh_project_is_created_linked_and_set_up():
    fake = FakeGcp()
    run = Run(fake)
    summary = await run.go(create_project=True)
    assert ("POST", "https://cloudresourcemanager.googleapis.com/v3/projects") in fake.calls
    assert fake.billing == BILLING and fake.sa
    assert fake.apis_enabled == ["compute.googleapis.com", "iam.googleapis.com"]
    assert {"role": "roles/owner", "members": [SA]} in fake.project_policy["bindings"]
    assert {"role": "roles/billing.costsManager", "members": [SA]} in fake.billing_policy["bindings"]
    assert run.stored == {"gcp_key": KEY_JSON}
    assert "limit 4, usage 1" in summary and PROJECT in summary and "gcp_key" in summary
    assert "factory-infra@" in summary and "roles/owner" in summary and "2 APIs" in summary


async def test_a_second_run_finds_everything_in_place():
    fake = FakeGcp()
    await Run(fake).go(create_project=True)
    again = Run(fake)
    await again.go(create_project=True)  # project, SA and bindings now exist: 409s and no-ops
    assert len([b for b in fake.project_policy["bindings"] if b["role"] == "roles/owner"]) == 1
    assert fake.project_policy["bindings"][0]["members"] == [SA]
    assert not any(url.endswith(":setIamPolicy") for method, url in fake.calls[len(fake.calls) // 2 :])
    assert any("already existed" in line for line in again.logs)


async def test_an_existing_project_is_verified_not_created():
    fake = FakeGcp(project=True, billing=BILLING)
    await Run(fake).go()
    assert ("POST", "https://cloudresourcemanager.googleapis.com/v3/projects") not in fake.calls


async def test_a_missing_project_fails_naming_the_step():
    fake = FakeGcp(fail={"GET https://cloudresourcemanager.googleapis.com/v3/projects/": 403})
    run = Run(fake)
    with pytest.raises(_gcp.StepError, match="step 'project'.*Already in place: nothing"):
        await run.go()
    assert fake.revoked == [TOKEN]


async def test_billing_linked_to_another_account_is_not_moved():
    fake = FakeGcp(project=True, billing="AAAAAA-BBBBBB-CCCCCC")
    with pytest.raises(_gcp.StepError, match="step 'billing'.*not billingAccounts/"):
        await Run(fake).go()
    assert fake.billing == "AAAAAA-BBBBBB-CCCCCC"


async def test_without_an_explicit_billing_account_roles_use_the_linked_account():
    fake = FakeGcp(project=True, billing="AAAAAA-BBBBBB-CCCCCC")
    run = Run(fake)
    summary = await run.go(billing_account="")
    assert any("billingAccounts/AAAAAA-BBBBBB-CCCCCC" in url and "IamPolicy" in url for _, url in fake.calls)
    assert "billing roles roles/billing.costsManager on AAAAAA-BBBBBB-CCCCCC" in summary
    assert fake.billing_policy["bindings"] == [{"role": "roles/billing.costsManager", "members": [SA]}]
    assert "already linked" in " ".join(run.logs)


async def test_no_permission_on_a_discovered_billing_account_skips_its_roles():
    """qa: the person signing in may not administer the account the project is linked to."""
    fake = FakeGcp(
        project=True,
        billing="AAAAAA-BBBBBB-CCCCCC",
        fail={"billingAccounts/AAAAAA-BBBBBB-CCCCCC:getIamPolicy": 403},
    )
    summary = await Run(fake).go(billing_account="")
    assert "billing roles skipped (no permission on linked account AAAAAA-BBBBBB-CCCCCC)" in summary
    assert "key stored as Flyte secret" in summary


async def test_no_permission_on_an_explicit_billing_account_still_fails():
    fake = FakeGcp(project=True, billing=BILLING, fail={f"billingAccounts/{BILLING}:getIamPolicy": 403})
    with pytest.raises(_gcp.StepError, match="billing roles"):
        await Run(fake).go(billing_account=BILLING)


async def test_an_unlinked_project_with_no_billing_account_fails():
    with pytest.raises(_gcp.StepError, match="not linked to billing"):
        await Run(FakeGcp(project=True)).go(billing_account="")


async def test_an_existing_conditional_binding_is_kept_and_an_unconditional_one_added():
    conditional = {"role": "roles/owner", "members": ["user:a@example.com"], "condition": {"title": "t"}}
    fake = FakeGcp(project=True, billing=BILLING, bindings=[conditional])
    await Run(fake).go()
    assert conditional in fake.project_policy["bindings"]
    assert {"role": "roles/owner", "members": [SA]} in fake.project_policy["bindings"]


async def test_joining_an_existing_role_binding_keeps_its_other_members():
    fake = FakeGcp(
        project=True, billing=BILLING, bindings=[{"role": "roles/owner", "members": ["user:a@x.com"]}]
    )
    await Run(fake).go()
    assert fake.project_policy["bindings"] == [{"role": "roles/owner", "members": ["user:a@x.com", SA]}]


async def test_a_policy_changed_under_us_is_read_again(monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True, billing=BILLING)
    real = fake._route
    state = {"conflicts": 1}

    def route(method, url, body):
        if url.endswith("/v3/projects/" + PROJECT + ":setIamPolicy") and state["conflicts"]:
            state["conflicts"] -= 1
            return fake._error(409)
        return real(method, url, body)

    monkeypatch.setattr(fake, "_route", route)
    await Run(fake).go()
    assert any(b["role"] == "roles/owner" for b in fake.project_policy["bindings"])


async def test_a_new_service_account_unknown_to_iam_yet_is_retried(monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True, billing=BILLING)
    real = fake._route
    state = {"failures": 2}

    def route(method, url, body):
        if url.endswith("/keys") and method == "POST" and state["failures"]:
            state["failures"] -= 1
            return fake._error(404)
        return real(method, url, body)

    monkeypatch.setattr(fake, "_route", route)
    run = Run(fake)
    await run.go()
    assert run.stored == {"gcp_key": KEY_JSON}


async def test_rotation_stores_the_new_key_then_deletes_the_previous_one():
    old = f"projects/{PROJECT}/serviceAccounts/x/keys/old"
    fake = FakeGcp(project=True, billing=BILLING, sa=True, keys=[old])
    fake.labels["factory-key-" + hashlib.sha256(b"gcp_key").hexdigest()[:32]] = "old"
    run = Run(fake)
    await run.go()
    assert fake.keys == [f"projects/{PROJECT}/serviceAccounts/x/keys/new"]
    delete = [i for i, (m, u) in enumerate(fake.calls) if m == "DELETE"]
    create = [i for i, (m, u) in enumerate(fake.calls) if m == "POST" and u.endswith("/keys")]
    assert create[0] < delete[0]
    assert "deleted 1 previous key" in " ".join(run.logs)


async def test_a_failed_secret_write_deletes_the_new_key_and_keeps_the_old_one():
    old = f"projects/{PROJECT}/serviceAccounts/x/keys/old"
    fake = FakeGcp(project=True, billing=BILLING, sa=True, keys=[old])
    run = Run(fake, store_error=RuntimeError("secret store down"))
    with pytest.raises(_gcp.StepError, match="step 'key'.*secret store down"):
        await run.go()
    assert fake.keys == [old]
    assert fake.revoked == [TOKEN]


async def test_the_failure_names_what_was_already_created():
    fake = FakeGcp(project=True, billing=BILLING, fail={"serviceAccounts": 500})
    with pytest.raises(_gcp.StepError) as info:
        await Run(fake).go()
    text = str(info.value)
    assert "step 'service account'" in text
    assert f"project {PROJECT} already existed" in text and "enabled 2 APIs" in text
    assert "safe to re-run" in text


async def test_the_quota_being_unavailable_does_not_fail_the_run():
    fake = FakeGcp(project=True, billing=BILLING, fail={"/regions/": 403})
    run = Run(fake)
    summary = await run.go()
    assert "quota unavailable" in summary and run.stored


async def test_a_quota_missing_from_the_region_is_reported_as_such():
    fake = FakeGcp(project=True, billing=BILLING)
    summary = await Run(fake).go(region="europe-west4")
    assert "limit 4" in summary  # the fake serves the same quotas for any region
    assert await _gcp.l4_quota(
        _gcp.Api(
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"quotas": []}))
            ),
            TOKEN,
        ),
        PROJECT,
        "us-west1",
    ) == ("PREEMPTIBLE_NVIDIA_L4_GPUS is not listed for us-west1")


async def test_the_token_is_revoked_on_success():
    fake = FakeGcp(project=True, billing=BILLING)
    await Run(fake).go()
    assert fake.revoked == [TOKEN]
    assert fake.calls[-1] == ("POST", _gcp.REVOKE_URL)


async def test_a_failing_revoke_is_reported_not_raised():
    fake = FakeGcp(project=True, billing=BILLING, fail={"oauth2.googleapis.com/revoke": 500})
    run = Run(fake)
    await run.go()
    assert any("revoke: HTTP 500" in line for line in run.logs)


async def test_the_owner_token_never_reaches_logs_errors_secrets_or_disk(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for fail in ({}, {"serviceAccounts": 500}):
        fake = FakeGcp(project=True, billing=BILLING, fail=fail)
        run = Run(fake)
        try:
            await run.go()
        except _gcp.StepError as exc:
            assert TOKEN not in str(exc) and TOKEN not in repr(exc.__cause__)
        assert not any(TOKEN in line for line in run.logs)
        assert all(TOKEN not in v for v in run.stored.values())
        assert fake.revoked == [TOKEN]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert not list(tmp_path.rglob("*"))  # nothing written anywhere


async def test_the_private_key_is_not_logged():
    run = Run(FakeGcp(project=True, billing=BILLING))
    summary = await run.go()
    assert "PRIVATE-KEY-MATERIAL" not in summary + " ".join(run.logs)


async def test_more_apis_than_one_batch_are_split():
    fake = FakeGcp(project=True, billing=BILLING)
    await Run(fake).go(apis=[f"svc{i}" for i in range(25)])
    assert len(fake.apis_enabled) == 25
    assert sum(1 for _, u in fake.calls if u.endswith(":batchEnable")) == 2


def test_defaults_match_the_brief():
    assert _gcp.DEFAULT_ROLES == ("roles/owner",)
    assert _gcp.DEFAULT_BILLING_ROLES == ("roles/billing.costsManager",)
    assert len(_gcp.DEFAULT_APIS) == 14 and "billingbudgets" in _gcp.DEFAULT_APIS


# --- profile `[gcp]` ---------------------------------------------------------------------


EXAMPLE = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()


def test_the_gcp_table_is_parsed():
    gcp = _profile.parse(EXAMPLE).gcp
    assert gcp == _profile.Gcp(
        secret_name="my_gcp_infra_key", roles=("roles/owner",), apis=("compute", "container", "iam")
    )


def test_gcp_is_optional():
    text = EXAMPLE[: EXAMPLE.index("[gcp]")]
    assert _profile.parse(text).gcp is None
    assert _profile.parse(text + "[gcp]\n").gcp == _profile.Gcp()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('roles = ["roles/owner"]', 'roles = "roles/owner"', "[gcp] roles must be a list of strings"),
        ('roles = ["roles/owner"]', "roles = [3]", "[gcp] roles must be a list of strings"),
        ('roles = ["roles/owner"]', 'roles = ["owner"]', "[gcp] roles has invalid entries: owner"),
        (
            'apis = ["compute", "container", "iam"]',
            'apis = ["Compute API"]',
            "[gcp] apis has invalid entries",
        ),
        (
            'secret_name = "my_gcp_infra_key"',
            "secret_name = 3",
            "[gcp] secret_name must be a non-empty string",
        ),
        (
            'secret_name = "my_gcp_infra_key"',
            'secret_name = "k"\nrole = "x"',
            "unknown key(s) in [gcp]: role",
        ),
    ],
)
def test_gcp_values_are_validated(old, new, message):
    assert old in EXAMPLE
    with pytest.raises(_profile.ProfileError, match=re.escape(message)):
        _profile.parse(EXAMPLE.replace(old, new, 1))


# --- the task ----------------------------------------------------------------------------


def test_argument_then_profile_then_default():
    from flyte_agent_factory import agent

    assert agent._gcp_setting(["a"], ("p",), ("d",)) == ["a"]
    assert agent._gcp_setting([], ("p",), ("d",)) == ["p"]
    assert agent._gcp_setting([], (), ("d",)) == ["d"]


@pytest.fixture
def task_env(monkeypatch):
    """The task with conditions, secrets and Google all faked; returns what they saw."""
    from types import SimpleNamespace

    import flyte
    import flyte.remote

    from flyte_agent_factory import agent

    fake = FakeGcp(project=True, billing=BILLING)
    seen = SimpleNamespace(fake=fake, conditions=[], secrets={})

    class Condition:
        class wait:
            @staticmethod
            async def aio():
                return "the-code"

    async def new_condition(name, /, **kwargs):
        seen.conditions.append((name, kwargs))
        return Condition()

    async def create(**kw):
        seen.secrets[kw["name"]] = kw["value"]

    monkeypatch.setattr(flyte, "new_condition", SimpleNamespace(aio=new_condition))
    monkeypatch.setattr(flyte.remote.Secret, "create", SimpleNamespace(aio=create))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return agent, seen


async def test_the_task_shows_the_link_through_a_condition_and_stores_the_key(task_env):
    agent, seen = task_env
    result = await agent.gcp_bootstrap.func(project_id=PROJECT, billing_account=BILLING, secret_name="mine")
    [(name, kwargs)] = seen.conditions
    assert name == "gcp-login-a0-001" and kwargs["prompt_type"] == "markdown"
    assert "[Open the Google sign-in page](https://accounts.google.com/" in kwargs["prompt"]
    assert seen.secrets == {"mine": KEY_JSON}
    assert "mine" in result and TOKEN not in result
    assert seen.fake.revoked == [TOKEN]


async def test_the_task_takes_unset_arguments_from_the_profile(task_env, monkeypatch):
    agent, seen = task_env
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE))
    await agent.gcp_bootstrap.func(project_id=PROJECT, billing_account=BILLING)
    assert list(seen.secrets) == ["my_gcp_infra_key"]
    assert seen.fake.apis_enabled == [
        "compute.googleapis.com",
        "container.googleapis.com",
        "iam.googleapis.com",
    ]


async def test_the_task_without_a_project_id_does_nothing(task_env):
    agent, seen = task_env
    with pytest.raises(ValueError, match="project_id is required"):
        await agent.gcp_bootstrap.func(project_id="", secret_name="mine")
    assert seen.conditions == [] and seen.fake.calls == []


async def test_the_task_without_any_secret_name_refuses(task_env, monkeypatch):
    agent, seen = task_env
    monkeypatch.setattr(_session_config, "_PROFILE", None)
    with pytest.raises(ValueError, match="no secret name"):
        await agent.gcp_bootstrap.func(project_id=PROJECT)
    assert seen.conditions == []


async def test_rotation_preserves_unrelated_keys_and_labels():
    old = f"projects/{PROJECT}/serviceAccounts/x/keys/old"
    unrelated = f"projects/{PROJECT}/serviceAccounts/x/keys/unrelated"
    fake = FakeGcp(project=True, billing=BILLING, keys=[old, unrelated])
    label = "factory-key-" + hashlib.sha256(b"gcp_key").hexdigest()[:32]
    fake.labels = {label: "old", "team": "infra"}
    await Run(fake).go()
    assert fake.keys == [unrelated, f"projects/{PROJECT}/serviceAccounts/x/keys/new"]
    assert fake.labels == {label: "new", "team": "infra"}


@pytest.mark.parametrize(
    "quota",
    [
        {"metric": _gcp.QUOTA_METRIC},
        {"metric": _gcp.QUOTA_METRIC, "limit": "odd", "usage": 0},
        {"metric": _gcp.QUOTA_METRIC, "limit": True, "usage": 0},
        None,
    ],
)
async def test_malformed_quota_is_informational(quota, monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True, billing=BILLING)
    real = fake._route

    def route(method, url, body):
        if "/regions/" in url:
            return httpx.Response(200, json={"quotas": [quota]})
        return real(method, url, body)

    monkeypatch.setattr(fake, "_route", route)
    run = Run(fake)
    assert "quota unavailable" in await run.go()
    assert run.stored == {"gcp_key": KEY_JSON}
    assert fake.revoked == [TOKEN]


@pytest.mark.parametrize(
    ("target", "status", "message"),
    [
        ("GET billingInfo", 404, "project not found"),
        ("PUT billingInfo", 403, "permission propagation"),
        ("POST :batchEnable", 400, "FAILED_PRECONDITION billing must be enabled"),
        ("POST /serviceAccounts", 403, "SERVICE_DISABLED"),
    ],
)
async def test_creation_propagation_is_retried(target, status, message, monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True)
    real = fake._route
    remaining = 2

    def route(method, url, body):
        nonlocal remaining
        verb, suffix = target.split()
        if method == verb and url.endswith(suffix) and remaining:
            remaining -= 1
            return httpx.Response(status, json={"error": {"message": message}})
        return real(method, url, body)

    monkeypatch.setattr(fake, "_route", route)
    await Run(fake).go()
    assert remaining == 0


@pytest.mark.parametrize("suffix", [":batchEnable", "/serviceAccounts", ":setIamPolicy"])
async def test_invalid_arguments_are_not_retried(suffix, monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True, billing=BILLING)
    real = fake._route
    failures = 0

    def route(method, url, body):
        nonlocal failures
        if method == "POST" and url.endswith(suffix):
            failures += 1
            return httpx.Response(400, json={"error": {"message": "INVALID_ARGUMENT bad role"}})
        return real(method, url, body)

    monkeypatch.setattr(fake, "_route", route)
    with pytest.raises(_gcp.StepError, match="INVALID_ARGUMENT"):
        await Run(fake).go()
    assert failures == 1


async def test_billing_policy_requests_v3_and_preserves_conditions(monkeypatch: pytest.MonkeyPatch):
    fake = FakeGcp(project=True, billing=BILLING)
    conditional = {
        "role": "roles/billing.costsManager",
        "members": ["user:a@x.com"],
        "condition": {"title": "t", "expression": "true"},
    }
    fake.billing_policy = {"version": 3, "etag": "b", "bindings": [conditional]}
    real = fake.handler
    reads = []

    def handler(request):
        if "billingAccounts/" in str(request.url) and request.url.path.endswith(":getIamPolicy"):
            reads.append(request.url.params["options.requestedPolicyVersion"])
        return real(request)

    monkeypatch.setattr(fake, "handler", handler)
    await Run(fake).go()
    assert reads == ["3"]
    assert conditional in fake.billing_policy["bindings"]
    assert {"role": "roles/billing.costsManager", "members": [SA]} in fake.billing_policy["bindings"]
    assert fake.billing_policy["version"] == 3


async def test_operations_can_take_more_than_three_minutes():
    polls = 0

    class SlowApi:
        async def call(self, method, url):
            nonlocal polls
            polls += 1
            return {"name": "operations/slow", "done": polls == 100}

    await _gcp._wait_operation(
        cast("_gcp.Api", SlowApi()),  # only `call` is used
        "https://example.com",
        {"name": "operations/slow"},
        no_sleep,
        "slow operation",
    )
    assert polls == 100


async def test_propagation_retries_have_a_bounded_backoff():
    attempts = 0
    delays = []

    async def call():
        nonlocal attempts
        attempts += 1
        raise _gcp.GcpError("project not found", 404)

    async def sleep(delay):
        delays.append(delay)

    with pytest.raises(_gcp.GcpError, match="project not found"):
        await _gcp._retrying(call, sleep, (404,))
    assert attempts == 6
    assert delays == [4, 8, 16, 30, 30]
    assert sum(delays) < 120


async def test_an_unrecorded_key_is_not_deleted():
    old = f"projects/{PROJECT}/serviceAccounts/x/keys/unrecorded"
    fake = FakeGcp(project=True, billing=BILLING, keys=[old])
    await Run(fake).go()
    assert fake.keys == [old, f"projects/{PROJECT}/serviceAccounts/x/keys/new"]
    assert not any(method == "DELETE" for method, _ in fake.calls)
