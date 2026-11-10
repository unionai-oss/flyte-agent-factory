"""The credential sealing page: served by factory-mcp at `_grant_seal.PAGE_PATH`, static, makes no requests.

The owner opens a link whose fragment carries the project's public key (see `docs/credential-grants.md`; the
fragment never reaches a server), pastes a credential, and the page seals it in the browser with WebCrypto, in
the `fgc1` envelope that `_grant_seal.KeyPair.open` reads. The code bundle ships only `.py` files, so the page
is a string here, not an `.html` file.

`GRANT_JS` is one script: `sealCredential` and `checkCredential` are plain functions that run in node 20+
unchanged (the tests drive them there), and the UI wiring does nothing when there is no `document`.
The page's Content-Security-Policy pins the script and the style by hash, computed from these strings at
import.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from flyte_agent_factory._grants import MAX_HOURS


def _render_hour_options() -> str:
    """Return the JS array of expiry choices: the usual steps below the cap, then the cap itself."""
    hours = [h for h in (1, 2, 4, 8, 12, 24, 72) if h < MAX_HOURS] + [int(MAX_HOURS)]
    return json.dumps(
        [
            [h, f"{h // 24} days" if h >= 168 and h % 24 == 0 else f"{h} hour{'s' if h != 1 else ''}"]
            for h in hours
        ]
    )


GRANT_JS = r"""
"use strict";
const HKDF_INFO = "flyte-agent-factory credential grant v1";
const HOUR_OPTIONS = __HOUR_OPTIONS__;
const NAME_RE = /^[a-z][a-z0-9-]{0,39}$/;
const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d$/;
const DAMAGED = "this link is damaged: copy the whole link again";

function b64uEncode(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64uDecode(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

// The sealed text for `payload` (an object), openable only with the private key behind `publicB64`, and only
// for `project`. Same steps as `_grant_seal.seal`.
async function sealCredential(publicB64, kid, project, payload) {
  const subtle = globalThis.crypto.subtle;
  const text = new TextEncoder();
  let raw;
  try {
    raw = b64uDecode(publicB64);
  } catch (err) {
    throw new Error(DAMAGED);
  }
  const digest = new Uint8Array(await subtle.digest("SHA-256", raw));
  if (raw.length !== 65 || raw[0] !== 4 || b64uEncode(digest).slice(0, 16) !== kid) throw new Error(DAMAGED);
  const peer = await subtle.importKey("raw", raw, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const ephemeral = await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const secret = await subtle.deriveBits({ name: "ECDH", public: peer }, ephemeral.privateKey, 256);
  const hkdf = await subtle.importKey("raw", secret, "HKDF", false, ["deriveKey"]);
  const key = await subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: text.encode(HKDF_INFO) },
    hkdf, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const ct = await subtle.encrypt(
    { name: "AES-GCM", iv: iv, additionalData: text.encode("fgc1|" + kid + "|" + project) },
    key, text.encode(JSON.stringify(payload)));
  const epk = new Uint8Array(await subtle.exportKey("raw", ephemeral.publicKey));
  const envelope = { v: 1, kid: kid, epk: b64uEncode(epk), iv: b64uEncode(iv),
    ct: b64uEncode(new Uint8Array(ct)) };
  return "fgc1." + b64uEncode(text.encode(JSON.stringify(envelope)));
}

// {error, warning} for a pasted credential; error means do not seal. The supervisor checks again.
function checkCredential(value) {
  const text = value.trim();
  if (!text) return { error: "Paste the credential first." };
  // Any AKIA key is refused, session token or not: the project's check (parse_aws) does the same.
  if (/\bAKIA[0-9A-Z]{16}\b/.test(text)) {
    return { error: "This is a long-lived access key (AKIA...), which is not accepted. " +
      "Use a temporary credential instead: IAM Identity Center \"Access keys\" or aws sts assume-role." };
  }
  if (!/\bASIA[0-9A-Z]{16}\b/.test(text)) {
    return { warning: "No temporary access key id (ASIA...) found. Sealing anyway; " +
      "the project checks it again." };
  }
  return {};
}

function nearestHours(hours) {
  const wanted = Number.isFinite(hours) && hours > 0 ? hours : 4;
  let best = HOUR_OPTIONS[0][0];
  for (const [option] of HOUR_OPTIONS) {
    if (Math.abs(option - wanted) < Math.abs(best - wanted)) best = option;
  }
  return best;
}

function wirePage() {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const link = { project: params.get("p") || "", name: params.get("n") || "", key: params.get("k") || "",
    kid: params.get("kid") || "", hours: parseFloat(params.get("h")) };
  const status = $("status");
  const say = (message, bad) => {
    status.textContent = message;
    status.className = bad ? "bad" : "";
  };
  if (params.get("v") !== "1" || !link.project || !NAME_RE.test(link.name) || !link.key || !link.kid) {
    $("form").hidden = true;
    say(DAMAGED, true);
    return;
  }
  $("name").textContent = link.name;
  $("project").textContent = link.project;
  for (const [hours, label] of HOUR_OPTIONS) {
    const option = document.createElement("option");
    option.value = String(hours);
    option.textContent = label;
    $("hours").appendChild(option);
  }
  $("hours").value = String(nearestHours(link.hours));

  $("seal").addEventListener("click", async () => {
    const region = $("region").value.trim();
    const checked = checkCredential($("credential").value);
    if (checked.error) return say(checked.error, true);
    if (region && !REGION_RE.test(region)) return say("The region should look like us-east-1.", true);
    const now = Math.floor(Date.now() / 1000);
    const payload = { name: link.name, kind: "aws", expires_at: now + parseFloat($("hours").value) * 3600,
      sealed_at: now, value: $("credential").value.trim(), region: region };
    payload.expires_at = Math.floor(payload.expires_at);
    $("seal").disabled = true;
    try {
      $("sealed").value = await sealCredential(link.key, link.kid, link.project, payload);
      $("credential").value = "";
      $("result").hidden = false;
      say(checked.warning || "Sealed. The credential was cleared from this page.", false);
    } catch (err) {
      say("Could not seal: " + err.message, true);
    } finally {
      $("seal").disabled = false;
    }
  });

  $("copy").addEventListener("click", async () => {
    const box = $("sealed");
    try {
      await navigator.clipboard.writeText(box.value);
      say("Copied.", false);
    } catch (err) {
      box.focus();
      box.select();
      const done = document.execCommand && document.execCommand("copy");
      say(done ? "Copied." : "Select the text above and copy it.", !done);
    }
  });
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", wirePage);
""".replace("__HOUR_OPTIONS__", _render_hour_options())

GRANT_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0f1115; color: #f2f4f8; font: 16px/1.5 system-ui, sans-serif; }
main { max-width: 40rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
code { background: #1c2028; padding: .1rem .35rem; border-radius: 4px; }
label { display: block; margin: 1rem 0 .25rem; font-weight: 600; }
textarea, input, select { width: 100%; background: #1c2028; color: #f2f4f8; border: 1px solid #5b6577;
  border-radius: 6px; padding: .6rem; font: 16px/1.4 ui-monospace, monospace; }
textarea { min-height: 9rem; }
textarea:focus, input:focus, select:focus, button:focus-visible {
  outline: 3px solid #7cc4ff; outline-offset: 1px; }
button { margin-top: 1rem; padding: .7rem 1.4rem; font: inherit; font-weight: 700; color: #0f1115;
  background: #7cc4ff; border: 0; border-radius: 6px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
[hidden] { display: none; }
#status { margin: 1rem 0; min-height: 1.5rem; }
#status.bad { color: #ffb4a8; font-weight: 600; }
.note { color: #c3cad6; font-size: .95rem; }
"""

GRANT_BODY = """
<main>
<h1>Give credential <code id="name"></code> to project <code id="project"></code></h1>
<p class="note">This page seals the credential in your browser. Nothing is sent anywhere.</p>
<div id="form">
<label for="credential">Credential</label>
<textarea id="credential" spellcheck="false" autocomplete="off" autocapitalize="off"
 placeholder="export AWS_ACCESS_KEY_ID=ASIA...&#10;export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=..."></textarea>
<label for="hours">Access ends after</label>
<select id="hours"></select>
<label for="region">AWS region (optional)</label>
<input id="region" type="text" spellcheck="false" autocomplete="off" placeholder="us-east-1">
<button id="seal" type="button">Seal</button>
</div>
<p id="status" role="status" aria-live="polite"></p>
<div id="result" hidden>
<label for="sealed">Sealed text</label>
<textarea id="sealed" readonly spellcheck="false"></textarea>
<button id="copy" type="button">Copy</button>
<p class="note">Paste it back as the answer to the project's question (or into its <code>direct-</code>
inbox if the link came from <code>factory.credential_link</code>). Only that project can open it, and only
until it ends.</p>
</div>
<p class="note">Give a short-lived, scoped credential (IAM Identity Center &ldquo;Access keys&rdquo; or
<code>aws sts assume-role</code>), never a long-lived key. See docs/credential-grants.md, &ldquo;Which
credential to give&rdquo;.</p>
</main>
"""

PAGE_HTML = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>Give a credential</title>\n"
    f"<style>{GRANT_CSS}</style>\n</head>\n<body>{GRANT_BODY}"
    f"<script>{GRANT_JS}</script>\n</body>\n</html>\n"
)


def _hash_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


PAGE_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; script-src {_hash_source(GRANT_JS)}; style-src {_hash_source(GRANT_CSS)}; "
        "connect-src 'none'; img-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


async def serve_grant_page(request: Request) -> Response:
    return HTMLResponse(PAGE_HTML, headers=PAGE_HEADERS)


def register(server: Any) -> None:
    """Add the page to a FastMCP server, before its ASGI app is built.

    FastMCP serves custom routes inside its own app, which the MCP environment mounts at `/flyte-mcp`, so
    this one is public at `_grant_seal.PAGE_PATH`.
    """
    server.custom_route("/grant", methods=["GET"])(serve_grant_page)
