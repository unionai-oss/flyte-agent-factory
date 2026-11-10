"""The sealed envelope (`fgc1`): a credential from the owner's browser to one project's supervisor.

The supervisor holds an ECDH P-256 key pair that exists only in its memory (`KeyPair`). The owner's page, or
`seal` here for an owner without a browser, encrypts the credential to the public key: an ephemeral ECDH key
agreement, HKDF-SHA256 and AES-256-GCM, with the project and key id bound in as associated data. Only that
process can open it, and once it exits nothing can. The format is in `docs/credential-grants.md`; the page's
JavaScript (`_grant_page`) implements the same steps with WebCrypto.

Errors are `GrantError`, whose message is safe to show anywhere: it never quotes the sealed text or a value.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: What every sealed text starts with; how the supervisor tells one from a message.
SEAL_PREFIX = "fgc1."
_INFO = b"flyte-agent-factory credential grant v1"
#: A credential name: what `credential-grant` and the link use.
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
KINDS = ("aws",)
#: Where the page is served on the factory-mcp app, below its public URL.
PAGE_PATH = "/flyte-mcp/grant"
#: A sealed text far larger than any credential is refused before it is decoded.
_MAX_SEALED = 64 * 1024


class GrantError(ValueError):
    """A credential that cannot be granted, with a reason safe to publish (no value, no sealed text)."""


def encode_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_key_id(public_raw: bytes) -> str:
    return encode_b64url(hashlib.sha256(public_raw).digest())[:16]


def _build_aad(kid: str, project: str) -> bytes:
    return f"fgc1|{kid}|{project}".encode()


def _derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=_INFO).derive(shared)


@dataclass
class KeyPair:
    """One supervisor's key pair. The private key is never serialised, so it dies with the process."""

    _private: ec.EllipticCurvePrivateKey | None = field(repr=False)
    _public_raw: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if self._private is None:
            raise ValueError("a KeyPair needs a private key")
        self._public_raw = self._private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )

    @classmethod
    def generate(cls) -> KeyPair:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @property
    def public_raw(self) -> bytes:
        return self._public_raw

    @property
    def public_b64(self) -> str:
        return encode_b64url(self.public_raw)

    @property
    def kid(self) -> str:
        return derive_key_id(self.public_raw)

    def open(self, sealed: str, project: str) -> dict[str, Any]:
        """Return the sealed payload, checked; `GrantError` if it is not for this key and project."""
        envelope = _decode_envelope(sealed)
        if self._private is None:
            raise GrantError(
                "this project's grant key is gone (the project is ending); nothing can be granted"
            )
        if envelope["kid"] != self.kid:
            raise GrantError(
                "this sealed credential was made for another key: another project, or this project before "
                "a restart. Open a new link from this project and seal it again."
            )
        try:
            peer = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), decode_b64url(envelope["epk"])
            )
            key = _derive(self._private.exchange(ec.ECDH(), peer))
            plain = AESGCM(key).decrypt(
                decode_b64url(envelope["iv"]),
                decode_b64url(envelope["ct"]),
                _build_aad(envelope["kid"], project),
            )
        except (InvalidTag, ValueError, binascii.Error) as exc:
            raise GrantError(
                f"this sealed credential does not open for project {project!r}: it was altered, or sealed "
                "for another project"
            ) from exc
        try:
            payload = json.loads(plain)
        except ValueError as exc:
            raise GrantError("the sealed credential holds no readable payload") from exc
        return check_payload(payload)

    def wipe(self) -> None:
        """Drop the private key: nothing sealed to it opens afterwards in this process either."""
        self._private = None


def _decode_envelope(sealed: str) -> dict[str, str]:
    sealed = "".join(sealed.split())  # a paste can wrap lines
    if not sealed.startswith(SEAL_PREFIX):
        raise GrantError(f"not a sealed credential: it must start with {SEAL_PREFIX!r}")
    if len(sealed) > _MAX_SEALED:
        raise GrantError("the sealed credential is too large")
    try:
        envelope = json.loads(decode_b64url(sealed[len(SEAL_PREFIX) :]))
    except (ValueError, binascii.Error) as exc:
        raise GrantError("the sealed credential is damaged: copy all of it again") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != 1:
        raise GrantError("the sealed credential has an unknown version")
    for part in ("kid", "epk", "iv", "ct"):
        if not isinstance(envelope.get(part), str) or not envelope[part]:
            raise GrantError("the sealed credential is damaged: copy all of it again")
    return envelope


def compute_seal_id(sealed: str) -> str:
    """Return a digest of the parts AES-GCM authenticates, so a re-encoded copy of the text has the same id.

    The outer envelope is unauthenticated JSON: key order and spacing change the text but not the ciphertext.
    """
    envelope = _decode_envelope(sealed)
    digest = hashlib.sha256()
    for part in ("kid", "epk", "iv", "ct"):
        raw = decode_b64url(envelope[part]) if part != "kid" else envelope[part].encode()
        digest.update(len(raw).to_bytes(4, "big") + raw)  # length-prefixed: no field can bleed into the next
    return digest.hexdigest()


#: Sealed text inside a message: the prefix, then base64url, possibly hard-wrapped over several lines.
_SEALED_RUN = re.compile(re.escape(SEAL_PREFIX) + r"[A-Za-z0-9_-]+(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9_-]+)*")


def find_sealed(text: str) -> str | None:
    """Return the sealed text inside `text` (quoted, in backticks, wrapped, with words around it), or None.

    A wrapped run may have swallowed a following line of prose, so the longest run of lines that decodes to
    an envelope wins. When none does, the whole run is returned so that `KeyPair.open` says it is damaged.
    """
    match = _SEALED_RUN.search(text)
    if match is None:
        return None
    lines = [line.strip() for line in match.group(0).splitlines()]
    for count in range(len(lines), 0, -1):
        candidate = "".join(lines[:count])
        try:
            _decode_envelope(candidate)
        except GrantError:
            continue
        return candidate
    return "".join(lines)


def check_payload(payload: Any) -> dict[str, Any]:
    """Return `payload` if it has the fields the format requires, with the right types; else `GrantError`."""
    if not isinstance(payload, dict):
        raise GrantError("the sealed credential holds no readable payload")
    name, kind = payload.get("name"), payload.get("kind")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise GrantError("the sealed credential has no valid name")
    if kind not in KINDS:
        raise GrantError(f"credential kind {kind!r} is not supported (supported: {', '.join(KINDS)})")
    for number in ("expires_at", "sealed_at"):
        if not isinstance(payload.get(number), int) or isinstance(payload.get(number), bool):
            raise GrantError(f"the sealed credential has no valid {number}")
    if not isinstance(payload.get("value"), str) or not payload["value"].strip():
        raise GrantError("the sealed credential is empty")
    region = payload.get("region", "")
    if not isinstance(region, str) or (region and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region)):
        raise GrantError("the sealed credential has an invalid region")
    return {**payload, "region": region}


def seal(public_b64: str, project: str, payload: dict[str, Any]) -> str:
    """Seal `payload` to the public key, as the page does. For the no-browser fallback and for tests."""
    public_raw = decode_b64url(public_b64)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_raw)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    key = _derive(ephemeral.exchange(ec.ECDH(), peer))
    kid = derive_key_id(public_raw)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(check_payload(payload)).encode(), _build_aad(kid, project))
    envelope = {
        "v": 1,
        "kid": kid,
        "epk": encode_b64url(
            ephemeral.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        ),
        "iv": encode_b64url(iv),
        "ct": encode_b64url(ct),
    }
    return SEAL_PREFIX + encode_b64url(json.dumps(envelope, separators=(",", ":")).encode())


@dataclass(frozen=True)
class Link:
    """What a grant link carries in its fragment. None of it is secret."""

    project: str
    name: str
    public_b64: str
    kid: str
    hours: float


def make_link(base_url: str, link: Link) -> str:
    fragment = urllib.parse.urlencode(
        {
            "v": "1",
            "p": link.project,
            "n": link.name,
            "k": link.public_b64,
            "kid": link.kid,
            "h": f"{link.hours:g}",
        }
    )
    return f"{base_url.rstrip('/')}{PAGE_PATH}#{fragment}"


def parse_link(url: str) -> Link:
    fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url.strip()).fragment))
    try:
        link = Link(fields["p"], fields["n"], fields["k"], fields["kid"], float(fields.get("h") or 4))
        public_raw = decode_b64url(link.public_b64)
    except (KeyError, ValueError) as exc:  # binascii.Error is a ValueError
        raise GrantError("not a credential grant link (its fragment lacks p, n, k or kid)") from exc
    # An uncompressed P-256 point, as the page also requires.
    if len(public_raw) != 65 or public_raw[0] != 0x04:
        raise GrantError("the link's key is damaged: copy the whole link again")
    if derive_key_id(public_raw) != link.kid or not NAME_RE.fullmatch(link.name):
        raise GrantError("the link's key or name is damaged: copy the whole link again")
    return link
