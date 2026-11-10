"""Exception tuples for best-effort calls, so a handler names what the call can raise."""

from __future__ import annotations

import httpx
from connectrpc.errors import ConnectError

# obstore is a hard dependency of flyte (its object-store backend), so this import cannot fail
# where flyte imports.
from obstore.exceptions import BaseError as ObstoreError

#: What a call into Flyte's control plane or object store raises. `flyte.errors.*` are all
#: RuntimeError subclasses and an RPC failure is a ConnectError. Object storage raises obstore's
#: own errors -- `flyte.storage.put`, `Dir.from_local` and fsspec reads and writes raise
#: `obstore.exceptions.GenericError`, which is not an OSError (flyte itself catches
#: `(OSError, GenericError)`) -- or OSError; sockets raise OSError (TimeoutError included) or
#: httpx.HTTPError, and a half-formed response surfaces as ValueError (JSON included) or
#: KeyError. Anything else is a bug and should be loud.
REMOTE_ERRORS = (ConnectError, RuntimeError, OSError, ObstoreError, httpx.HTTPError, ValueError, KeyError)
