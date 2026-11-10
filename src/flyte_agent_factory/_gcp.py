"""Bootstrap a project's GCP credential from a sign-in link, keeping only a service-account key.

The owner clicks a link, signs in to Google, and pastes back the code Google shows. With that
one short-lived token this module prepares a project (create or verify it, billing, APIs), makes
a dedicated service account with only the roles the project needs, and returns one key for it.
The caller stores the key as a Flyte secret. The owner's token lives in memory for the length of
the run and is revoked at the end; it is never written, logged or stored.

Sign-in method: a direct OAuth 2.0 authorization-code flow with PKCE, rather than driving
`gcloud auth login --no-launch-browser`.
- `gcloud` would have to be in the image, or downloaded at run time (pinned version + sha256),
  and its prompt text would have to be scraped off a pty. The
  token would also land in gcloud's credential store on disk, which this flow must not do.
- The authorization-code flow is one URL and one POST. It uses the Cloud SDK's published
  installed-app client and the `sdk.cloud.google.com/authcode.html` redirect, the same page
  `gcloud auth login --no-launch-browser` sends people to, which just displays the code.
  Google's old out-of-band redirect (`urn:ietf:wg:oauth:2.0:oob`) is shut down and is not used.

Scope: `cloud-platform`. Creating a project, linking billing, enabling services and setting IAM
policy on a project and a billing account span several APIs; the narrower scopes (`cloud-billing`,
`iam`) do not cover Resource Manager or Service Usage, so there is no narrower set that works.

Every step is safe to repeat: a failure names the step and what was already done, and a re-run
picks up from there.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import math
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TypedDict, TypeVar, cast
from urllib.parse import urlencode

import httpx

# The Cloud SDK's installed-app client. Google publishes this id and secret in the open-source
# gcloud CLI; an installed-app "secret" is not confidential. PKCE is what protects the exchange.
CLIENT_ID = "32555940559.apps.googleusercontent.com"
CLIENT_SECRET = "ZmssLNjJy2998hD4CTg2ejr2"  # gitleaks:allow
REDIRECT_URI = "https://sdk.cloud.google.com/authcode.html"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"

SA_NAME = "factory-infra"
QUOTA_METRIC = "PREEMPTIBLE_NVIDIA_L4_GPUS"


def setting(
    requested: Iterable[str] | None, profile_value: Iterable[str] | None, builtin: Iterable[str]
) -> list[str]:
    """Return an argument, else the profile's `[gcp]` value, else the built-in default."""
    return list(requested or profile_value or builtin)


DEFAULT_ROLES = ("roles/owner",)
DEFAULT_BILLING_ROLES = ("roles/billing.costsManager",)
DEFAULT_APIS = (
    "compute",
    "container",
    "iam",
    "iamcredentials",
    "cloudresourcemanager",
    "serviceusage",
    "storage",
    "artifactregistry",
    "secretmanager",
    "billingbudgets",
    "run",
    "cloudscheduler",
    "logging",
    "monitoring",
)

_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_BILLING_ACCOUNT = re.compile(r"[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}")
# serviceusage accepts at most 20 services per batchEnable call.
_BATCH = 20
# IAM and service accounts are eventually consistent: a new account can be unknown for seconds.
_RETRIES = 6


class GcpError(RuntimeError):
    """An API call failed. Carries the HTTP status; the message never holds a credential."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class StepError(RuntimeError):
    """A bootstrap step failed. The message says which, and what was already in place."""


Ask = Callable[[str, int], Awaitable[str]]  # (markdown prompt, attempt) -> what the human signalled
Log = Callable[[str, str], None]  # (icon, text)
Sleep = Callable[[float], Awaitable[None]]


class _Token(TypedDict, total=False):
    access_token: str


class _Binding(TypedDict):
    role: str
    members: list[str]


class _ApiResponse(TypedDict, total=False):
    done: bool
    error: dict[str, object]
    name: str
    state: str
    billingAccountName: str
    billingEnabled: bool
    bindings: list[_Binding]
    labels: dict[str, str]
    privateKeyData: str
    quotas: list[dict[str, object]]


_T = TypeVar("_T")


def new_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(challenge: str) -> str:
    # No offline access is requested. Installed-app clients may still receive a refresh token;
    # it stays in memory, and revoking the access token revokes the whole grant.
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def sign_in_prompt(url: str, attempt: int) -> str:
    return (
        f"### Google sign-in for GCP bootstrap — attempt {attempt}\n\n"
        # An explicit markdown link: a bare OAuth URL is not clickable in the Union UI.
        f"- [Open the Google sign-in page]({url})\n\n"
        "Sign in as an owner of the project (and a billing admin, if it creates the project or "
        "grants billing roles), approve, then signal this condition with the **code** the page "
        "shows. Your token is used for this run only and revoked at the end.\n\n"
        "Signal `/abort` to give up."
    )


def _message(response: httpx.Response) -> str:
    """Return the API's own error text, without the request (which carries the bearer token)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if not isinstance(body, dict):
        return str(body)[:300]
    err = body.get("error")
    if isinstance(err, dict):
        return f"{err.get('status', '')} {err.get('message', '')}".strip()[:300]
    # OAuth errors: {"error": "invalid_grant", "error_description": "..."}
    return f"{err or ''} {body.get('error_description', '')}".strip()[:300]


async def exchange_code(http: httpx.AsyncClient, code: str, verifier: str) -> str:
    """Trade the pasted code for an access token."""
    response = await http.post(
        TOKEN_URL,
        data={
            "code": code.strip(),
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if response.status_code != 200:
        raise GcpError(f"code exchange failed: {_message(response)}", response.status_code)
    # Google defines access_token as a string; truthiness is checked below.
    token = cast(_Token, response.json()).get("access_token")
    if not token:
        raise GcpError("code exchange returned no access token")
    return token


async def sign_in(http: httpx.AsyncClient, ask: Ask, log: Log, attempts: int) -> str:
    """Show the link, take the code, return an access token. A bad or expired code gets a fresh link."""
    last = ""
    for attempt in range(1, attempts + 1):
        verifier, challenge = new_pkce()
        code = (await ask(sign_in_prompt(authorization_url(challenge), attempt), attempt)).strip()
        if code.lower() in ("/abort", "abort"):
            raise StepError("sign-in aborted")
        if not code:
            last = "no code was given"
        else:
            try:
                return await exchange_code(http, code, verifier)
            except GcpError as exc:
                last = str(exc)
        log("⏳", f"sign-in attempt {attempt}/{attempts} did not complete ({last})")
    raise StepError(f"sign-in did not complete in {attempts} attempts: {last}")


class Api:
    """Authenticated JSON calls. The token is held here and nowhere else."""

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    async def call(
        self, method: str, url: str, *, json: object = None, params: Mapping[str, str | int] | None = None
    ) -> _ApiResponse:
        response = await self._http.request(
            method,
            url,
            json=json,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if response.status_code >= 400:
            raise GcpError(
                f"{method} {url.split('?')[0]}: {response.status_code} {_message(response)}",
                response.status_code,
            )
        # These endpoints return Google resource objects with the fields read below.
        return cast(_ApiResponse, response.json()) if response.content else {}

    async def revoke(self) -> int:
        """Revoke the token with Google. Returns the HTTP status."""
        response = await self._http.post(
            REVOKE_URL,
            data={"token": self._token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return response.status_code


async def _retrying(  # type: ignore[return]  # The last retry re-raises, and _RETRIES is positive.
    call: Callable[[], Awaitable[_T]],
    sleep: Sleep,
    statuses: tuple[int, ...],
    transient: Callable[[GcpError], bool] | None = None,
) -> _T:
    for attempt in range(_RETRIES):
        try:
            return await call()
        except GcpError as exc:
            if exc.status not in statuses or (transient and not transient(exc)) or attempt == _RETRIES - 1:
                raise
            await sleep(min(4.0 * 2**attempt, 30.0))


async def _wait_operation(api: Api, base: str, op: _ApiResponse, sleep: Sleep, what: str) -> None:
    for _ in range(200):
        if op.get("done"):
            if op.get("error"):
                raise GcpError(f"{what} failed: {op['error'].get('message', op['error'])}")
            return
        await sleep(3.0)
        op = await api.call("GET", f"{base}/{op['name']}")
    raise GcpError(f"{what} did not finish in time")


def _short_api(name: str) -> str:
    return name if name.endswith(".googleapis.com") else f"{name}.googleapis.com"


async def ensure_project(api: Api, project_id: str, create: bool, sleep: Sleep) -> str:
    base = "https://cloudresourcemanager.googleapis.com/v3"
    created = False
    if create:
        try:
            op = await api.call(
                "POST", f"{base}/projects", json={"projectId": project_id, "displayName": project_id}
            )
            await _wait_operation(api, base, op, sleep, "project creation")
            created = True
        except GcpError as exc:
            if exc.status != 409:
                raise
    # Also the verification for an existing project, and for a 409 (which can mean the id is
    # someone else's: that surfaces here as a 403).
    project = await api.call("GET", f"{base}/projects/{project_id}")
    if project.get("state") != "ACTIVE":
        raise GcpError(f"project {project_id} is {project.get('state')}, not ACTIVE")
    return "created" if created else "already existed"


async def ensure_billing(
    api: Api, project_id: str, billing_account: str, sleep: Sleep = asyncio.sleep
) -> str:
    base = f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo"
    info = await _retrying(lambda: api.call("GET", base), sleep, (403, 404))
    linked = info.get("billingAccountName", "") if info.get("billingEnabled") else ""
    if linked and billing_account and linked != f"billingAccounts/{billing_account}":
        # Moving a project between billing accounts is the owner's call, not a bootstrap's.
        raise GcpError(f"project is linked to {linked}, not billingAccounts/{billing_account}")
    if linked:
        return f"already linked to {linked}"
    if not billing_account:
        raise GcpError("project is not linked to billing and no billing_account was given")
    await _retrying(
        lambda: api.call("PUT", base, json={"billingAccountName": f"billingAccounts/{billing_account}"}),
        sleep,
        (403, 404),
    )
    return f"linked to billingAccounts/{billing_account}"


async def enable_apis(api: Api, project_id: str, apis: list[str], sleep: Sleep) -> None:
    base = "https://serviceusage.googleapis.com/v1"
    ids = [_short_api(a) for a in apis]
    # batchEnable on a service that is already on is a no-op, so repeating it is safe.
    for i in range(0, len(ids), _BATCH):
        op = await _retrying(
            functools.partial(
                api.call,
                "POST",
                f"{base}/projects/{project_id}/services:batchEnable",
                json={"serviceIds": ids[i : i + _BATCH]},
            ),
            sleep,
            (400,),
            lambda exc: "FAILED_PRECONDITION" in str(exc) and "billing" in str(exc).lower(),
        )
        await _wait_operation(api, base, op, sleep, "enabling APIs")


async def ensure_service_account(api: Api, project_id: str, sleep: Sleep = asyncio.sleep) -> str:
    email = f"{SA_NAME}@{project_id}.iam.gserviceaccount.com"
    try:
        await _retrying(
            functools.partial(
                api.call,
                "POST",
                f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts",
                json={"accountId": SA_NAME, "serviceAccount": {"displayName": "Factory infrastructure"}},
            ),
            sleep,
            (400, 403),
            lambda exc: (
                "SERVICE_DISABLED" in str(exc)
                or "has not been used" in str(exc)
                or "not enabled" in str(exc).lower()
            ),
        )
    except GcpError as exc:
        if exc.status != 409:
            raise
    return email


async def _add_bindings(
    api: Api,
    read: Callable[[], Awaitable[_ApiResponse]],
    put: str,
    member: str,
    roles: list[str],
    sleep: Sleep,
) -> list[str]:
    """Add `member` to each role with a read-modify-write on the policy. Returns the roles newly granted."""
    added: list[str] = []
    for _ in range(_RETRIES):
        policy = await read()
        added = []
        bindings = policy.setdefault("bindings", [])
        for role in roles:
            # A conditional binding is not the grant we want, so only an unconditional one counts.
            binding = next((b for b in bindings if b["role"] == role and "condition" not in b), None)
            if binding is None:
                bindings.append({"role": role, "members": [member]})
                added.append(role)
            elif member not in binding["members"]:
                binding["members"].append(member)
                added.append(role)
        if not added:
            return []
        try:
            # A just-created service account can be unknown to IAM for a few seconds (400).
            await _retrying(
                functools.partial(api.call, "POST", put, json={"policy": policy}),
                sleep,
                (400,),
                lambda exc: "service account" in str(exc).lower() and "does not exist" in str(exc).lower(),
            )
            return added
        except GcpError as exc:
            if exc.status != 409:  # 409: the policy changed under us; read it again
                raise
            await sleep(1.0)
    raise GcpError(f"{put}: the policy kept changing")


async def grant_project_roles(
    api: Api, project_id: str, email: str, roles: list[str], sleep: Sleep
) -> list[str]:
    base = f"https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}"

    def read() -> Awaitable[_ApiResponse]:
        # Version 3 so existing conditional bindings come back intact and are written back as found.
        return api.call("POST", f"{base}:getIamPolicy", json={"options": {"requestedPolicyVersion": 3}})

    return await _add_bindings(api, read, f"{base}:setIamPolicy", f"serviceAccount:{email}", roles, sleep)


async def grant_billing_roles(
    api: Api, billing_account: str, email: str, roles: list[str], sleep: Sleep
) -> list[str]:
    base = f"https://cloudbilling.googleapis.com/v1/billingAccounts/{billing_account}"
    return await _add_bindings(
        api,
        lambda: api.call("GET", f"{base}:getIamPolicy", params={"options.requestedPolicyVersion": 3}),
        f"{base}:setIamPolicy",
        f"serviceAccount:{email}",
        roles,
        sleep,
    )


async def create_key(api: Api, project_id: str, email: str, sleep: Sleep) -> tuple[str, str]:
    """(key resource name, key JSON)."""
    base = f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts/{email}/keys"
    key = await _retrying(lambda: api.call("POST", base, json={}), sleep, (404,))
    return key["name"], base64.b64decode(key["privateKeyData"]).decode()


async def rotate_key_record(
    api: Api, project_id: str, secret_name: str, key_name: str, sleep: Sleep
) -> list[str]:
    # Flyte exposes secret metadata, not values. A project label records ownership without
    # storing credential material; preserve all other labels when updating this secret's record.
    base = f"https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}"
    label = "factory-key-" + hashlib.sha256(secret_name.encode()).hexdigest()[:32]
    project = await api.call("GET", base)
    labels = project.get("labels", {})
    previous = labels.get(label)
    current = key_name.rsplit("/", 1)[-1]
    labels[label] = current
    op = await api.call("PATCH", base, params={"updateMask": "labels"}, json={"labels": labels})
    await _wait_operation(
        api, "https://cloudresourcemanager.googleapis.com/v3", op, sleep, "recording key ownership"
    )
    if previous and previous != current:
        old_name = key_name.rsplit("/", 1)[0] + "/" + previous
        try:
            await api.call("DELETE", f"https://iam.googleapis.com/v1/{old_name}")
        except GcpError as exc:
            if exc.status != 404:
                raise
        return [previous]
    return []


async def l4_quota(api: Api, project_id: str, region: str) -> str:
    info = await api.call(
        "GET", f"https://compute.googleapis.com/compute/v1/projects/{project_id}/regions/{region}"
    )
    for quota in info.get("quotas", []):
        if quota.get("metric") == QUOTA_METRIC:
            limit, usage = quota.get("limit"), quota.get("usage")
            # The exact-type check excludes booleans before the numeric cast.
            if any(
                type(value) not in (int, float) or not math.isfinite(cast(float, value))
                for value in (limit, usage)
            ):
                raise ValueError("quota limit or usage is not a finite number")
            return f"{QUOTA_METRIC} in {region}: limit {limit:g}, usage {usage:g}"
    return f"{QUOTA_METRIC} is not listed for {region}"


def validate(project_id: str, billing_account: str, create_project: bool, region: str) -> None:
    """Refuse before any sign-in. A default project would be a guess about where to put an Owner key."""
    if not project_id.strip():
        raise ValueError("project_id is required: this grants a service account roles on that project only")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ValueError(f"project_id {project_id!r} is not a valid GCP project id")
    if billing_account and not _BILLING_ACCOUNT.fullmatch(billing_account):
        raise ValueError("billing_account must look like 012345-6789AB-CDEF01")
    if create_project and not billing_account:
        raise ValueError(
            "create_project needs a billing_account: most of the APIs cannot be enabled without one"
        )
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region):
        raise ValueError(f"region {region!r} does not look like a GCP region, e.g. us-west1")


async def bootstrap(
    *,
    project_id: str,
    billing_account: str,
    create_project: bool,
    region: str,
    secret_name: str,
    roles: list[str],
    billing_roles: list[str],
    apis: list[str],
    ask: Ask,
    store_secret: Callable[[str, str], Awaitable[None]],
    log: Log,
    http: httpx.AsyncClient,
    sleep: Sleep = asyncio.sleep,
    attempts: int = 3,
) -> str:
    validate(project_id, billing_account, create_project, region)
    if not secret_name:
        raise ValueError("no secret name: pass secret_name or set [gcp] secret_name in the profile")

    token = await sign_in(http, ask, log, attempts)
    api = Api(http, token)
    done: list[str] = []
    step = "sign-in"
    try:
        step = "project"
        done.append(f"project {project_id} {await ensure_project(api, project_id, create_project, sleep)}")
        log("✅", done[-1])

        step = "billing"
        done.append(f"billing: {await ensure_billing(api, project_id, billing_account, sleep)}")
        log("✅", done[-1])

        billing_discovered = not billing_account
        if not billing_account:
            info = await api.call(
                "GET", f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo"
            )
            billing_account = info["billingAccountName"].removeprefix("billingAccounts/")

        step = "enable APIs"
        await enable_apis(api, project_id, apis, sleep)
        done.append(f"enabled {len(apis)} APIs")
        log("✅", done[-1])

        step = "service account"
        email = await ensure_service_account(api, project_id, sleep)
        done.append(f"service account {email}")
        log("✅", done[-1])

        step = "project roles"
        added = await grant_project_roles(api, project_id, email, roles, sleep)
        done.append(
            f"project roles {', '.join(roles)} ({'granted ' + ', '.join(added) if added else 'already held'})"
        )
        log("✅", done[-1])

        billing_skipped = ""
        if billing_account:
            step = "billing roles"
            try:
                added = await grant_billing_roles(api, billing_account, email, billing_roles, sleep)
                done.append(
                    f"billing roles {', '.join(billing_roles)} "
                    f"({'granted ' + ', '.join(added) if added else 'already held'})"
                )
            except GcpError as exc:
                # An account we found rather than were given: the person signing in may not
                # administer it. Skip (budgets then need a billing admin) instead of failing.
                if not (billing_discovered and exc.status == 403):
                    raise
                billing_skipped = f"no permission on linked account {billing_account}"
                done.append(f"billing roles skipped ({billing_skipped})")
        else:
            billing_skipped = "no billing_account"
            done.append("billing roles skipped (no billing_account)")
        log("✅", done[-1])

        step = "key"
        key_name, key_json = await create_key(api, project_id, email, sleep)
        try:
            await store_secret(secret_name, key_json)
        except BaseException:
            # Nothing holds this key, so do not leave it valid in GCP.
            await api.call("DELETE", f"https://iam.googleapis.com/v1/{key_name}")
            raise
        done.append(f"key {key_name.rsplit('/', 1)[-1]} stored as secret {secret_name!r}")
        log("🔑", done[-1])

        step = "retire old keys"
        old = await rotate_key_record(api, project_id, secret_name, key_name, sleep)
        if old:
            done.append(f"deleted {len(old)} previous key(s)")
            log("🧹", done[-1])

        step = "quota"
        try:
            quota = await l4_quota(api, project_id, region)
        # Quota reporting must never fail bootstrap. Attribute/TypeError: a malformed quota entry.
        except (GcpError, httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            quota = f"quota unavailable ({exc})"
        log("ℹ️", quota)
    except Exception as exc:
        raise StepError(
            f"gcp_bootstrap failed at step {step!r}: {exc}. Already in place: "
            f"{'; '.join(done) or 'nothing'}. Every step is safe to re-run."
        ) from exc
    finally:
        # Success or not, the owner's token stops working here.
        try:
            status = await api.revoke()
            log("🔒" if status == 200 else "⚠️", f"owner token revoke: HTTP {status}")
        except httpx.HTTPError as exc:  # reported, and must not mask the real outcome
            log("⚠️", f"owner token revoke failed: {type(exc).__name__}")

    summary = (
        f"project {project_id}; service account {email}; project roles {', '.join(roles)}; "
        + (
            f"billing roles {', '.join(billing_roles)} on {billing_account}; "
            if billing_account and not billing_skipped
            else f"billing roles skipped ({billing_skipped}); "
        )
        + f"{len(apis)} APIs enabled; {quota}; key stored as Flyte secret {secret_name!r}. "
        "Delete the key and service account when the project is finished."
    )
    log("✅", summary)
    return summary
