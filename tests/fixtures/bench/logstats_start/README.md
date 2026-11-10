# Logstats

Summarises Common Log Format access logs (plain or gzipped) using only the standard library.

Run `python -m logstats FILE [FILE ...] [--top N] [--json]` from this directory; `-` reads stdin.
Run the tests with `python -m pytest -q`.

- The summary has the line counts, total bytes, status classes, the top paths and hosts, and the
  earliest and latest timestamps.
- `--json` prints the same content as one JSON object.
- Exit codes: 0 when at least one line parsed, 1 when none did, 2 for a usage or file error.
