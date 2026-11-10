"""Command line entry point: `python -m shortlinks serve` runs the HTTP service."""

import argparse
import json
import random
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shortlinks import ATOMIC_VISITS, FREE_DELETED_SLUGS

LOCK = threading.Lock()
# slug, url, created_at, visits, expires_at
Row = tuple[str, str, str, int, str | None]
MAX_EXPIRES_IN = 31536000


def serve(port: int, path: str) -> None:
    """Serve the link API on 127.0.0.1:`port`, storing links in the SQLite file at `path`."""
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS links (slug TEXT PRIMARY KEY, url TEXT, created_at TEXT, "
        "visits INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, expires_at TEXT)"
    )
    db.commit()

    def find(slug: str, *, deleted: bool = False) -> Row | None:
        with LOCK:
            query = "SELECT slug, url, created_at, visits, expires_at FROM links WHERE slug = ?"
            row: Row | None = db.execute(
                query + ("" if deleted else " AND deleted = 0"), (slug,)
            ).fetchone()
            return row

    def link(row: Row) -> dict[str, Any]:
        return {
            "slug": row[0],
            "url": row[1],
            "created_at": row[2],
            "visits": row[3],
            "expires_at": row[4],
        }

    def expired(row: Row) -> bool:
        return row[4] is not None and datetime.now(timezone.utc) >= datetime.fromisoformat(row[4])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(
            self, status: int, body: dict[str, Any] | None = None, location: str = ""
        ) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            )
            expires_in = body.get("expires_in")
            # bool is an int subclass, so `true` needs its own test.
            if expires_in is not None and (
                type(expires_in) is not int or not 1 <= expires_in <= MAX_EXPIRES_IN
            ):
                return self.reply(
                    400, {"error": "expires_in must be an integer from 1 to 31536000"}
                )
            slug = body.get("slug")
            while not slug or (not body.get("slug") and find(slug, deleted=True)):
                slug = "".join(random.choices(string.ascii_letters + string.digits, k=7))
            if find(slug, deleted=not FREE_DELETED_SLUGS):
                return self.reply(409, {"error": "slug taken"})
            now = datetime.now(timezone.utc)
            created = now.isoformat()
            expires_at = (
                None if expires_in is None else (now + timedelta(seconds=expires_in)).isoformat()
            )
            with LOCK:
                db.execute("DELETE FROM links WHERE slug = ?", (slug,))
                db.execute(
                    "INSERT INTO links (slug, url, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (slug, body["url"], created, expires_at),
                )
                db.commit()
            self.reply(
                201,
                {
                    "slug": slug,
                    "url": body["url"],
                    "created_at": created,
                    "expires_at": expires_at,
                },
            )

        def do_GET(self) -> None:
            if self.path.startswith("/links/"):
                row = find(self.path.removeprefix("/links/"))
                if not row:
                    return self.reply(404, {"error": "not found"})
                return (
                    self.reply(410, {"error": "expired"})
                    if expired(row)
                    else self.reply(200, link(row))
                )
            slug = self.path.removeprefix("/")
            row = find(slug)
            if not row:
                return self.reply(404, {"error": "not found"})
            if expired(row):
                return self.reply(410, {"error": "expired"})
            if ATOMIC_VISITS:
                with LOCK:
                    db.execute("UPDATE links SET visits = visits + 1 WHERE slug = ?", (slug,))
                    db.commit()
            else:
                seen = row[3]
                time.sleep(0.02)
                with LOCK:
                    db.execute("UPDATE links SET visits = ? WHERE slug = ?", (seen + 1, slug))
                    db.commit()
            self.reply(302, location=row[1])

        def do_DELETE(self) -> None:
            slug = self.path.removeprefix("/links/")
            if not find(slug):
                return self.reply(404, {"error": "not found"})
            with LOCK:
                db.execute("UPDATE links SET deleted = 1 WHERE slug = ?", (slug,))
                db.commit()
            self.reply(204)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    """Parse the command line and run the chosen command."""
    parser = argparse.ArgumentParser(prog="shortlinks")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve")
    run.add_argument("--port", type=int, default=8080)
    run.add_argument("--db", default="shortlinks.db")
    args = parser.parse_args()
    serve(args.port, args.db)


main()
