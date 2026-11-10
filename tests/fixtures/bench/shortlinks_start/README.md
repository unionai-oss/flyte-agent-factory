# Shortlinks

A Python standard library link shortener with SQLite storage.

Run `python -m shortlinks serve --port 8080 --db shortlinks.db` from this directory.
Run the tests with `python -m pytest -q`.

- `POST /links` with JSON `{"slug": "hello", "url": "https://example.com"}` creates a link (201), or returns 409 for a taken slug.
- `GET /links/<slug>` returns its slug, URL, creation time and visit count (200), or 404.
- `GET /<slug>` counts a visit and redirects to its URL (302), or returns 404.
- `DELETE /links/<slug>` deletes a link (204), or returns 404.
