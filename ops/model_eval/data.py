"""Report parsing and the on-disk experiment records (no remote calls)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# USD / million tokens: input, output, 5-minute cache write, cache read.
# Sources: https://platform.claude.com/docs/en/about-claude/pricing
# https://developers.openai.com/api/docs/pricing
# These are configurable API-equivalent prices, not subscription charges.
# The unpinned Codex rate is explicitly an assumption; Fable remains unknown.
PRICES: Mapping[str, list[float] | None] = {
    "codex:default": [1.75, 14, 0, 0.175],  # Assumes gpt-5.3-codex; OpenAI pricing source above.
    "claude:fable": None,  # No published rate; configure before comparing spend.
    "claude:claude-opus-4-6": [5, 25, 6.25, 0.5],
    "claude:claude-sonnet-4-6": [3, 15, 3.75, 0.3],
    "claude:claude-haiku-4-5-20251001": [1, 5, 1.25, 0.1],
    "opus": [5, 25, 6.25, 0.5],
    "sonnet": [3, 15, 3.75, 0.3],
    "haiku": [1, 5, 1.25, 0.1],
    "fable": None,  # No public list price; configure explicitly instead of inventing a rate.
    "gpt-5.4": [2.5, 15, 0, 0.25],
    "gpt-5.4-mini": [0.75, 4.5, 0, 0.075],
    "gpt-5.3-codex": [1.75, 14, 0, 0.175],
}


def model_key(engine: str, model: str | None) -> str:
    """Return the one spelling of a priced model: `engine:model`, whatever the caller had.

    The Codex usage trailer already says `codex:default`, and a config key may be
    `fable` or `claude:fable`; stripping an existing engine prefix once is what
    keeps all of them on the same table row.
    """
    model = model or "default"
    if model.startswith(engine + ":"):
        model = model.split(":", 1)[1]
    return f"{engine}:{model}"


def normalize_key(key: str) -> str:
    """Return a price-table key or `engine:model` string, normalised by `model_key`."""
    head, sep, rest = key.partition(":")
    if sep and head in ("claude", "codex"):
        return model_key(head, rest)
    return model_key("codex" if key.startswith("gpt-") else "claude", key)


def normalize_prices(table: Mapping[str, list[float] | None] | None) -> dict[str, list[float] | None]:
    """Return `table` with every key normalised by `normalize_key`."""
    return {normalize_key(k): v for k, v in (table or {}).items()}


PRICES = normalize_prices(PRICES)
LIMITS = [
    "Reports are bounded to 300 timeline rows; missing history is unknown, not zero.",
    "Author totals include in-pod helpers and are rounded; the author model is an assumption.",
    "Role spend uses table prices; CLI API-equivalent cost is separate, not subscription billing. Missing "
    "token types/engines make table cost unknown.",
    "Idle durations are cache-expiry proxies, not observed per-turn cache misses. Cache writes assume 5m "
    "TTL.",
    "Fallback spend is aggregated by role suffix, not attributable to individual switches.",
]
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)"
    r"|xox[abpr]-[A-Za-z0-9-]+|AIza[A-Za-z0-9_-]{30,}|sk-(?:ant-)?[A-Za-z0-9_-]{12,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|tskey-[A-Za-z0-9_-]{12,}"
    r"|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/-]{12,}"
)
_SECRET_KEY = re.compile(r"(?i:.*(?:token|secret|password|api_key)|private_key)")
# Only quoted assignments are unambiguous in prose. Source patches bypass this entirely.
_QUOTED_SECRET = re.compile(
    r"(?<![\w])[\"']?(?i:api[_-]?key|access[_-]?token|refresh[_-]?token|password|token|secret|CLAUDE_CODE_OAUTH_TOKEN)[\"']?\s*[=:]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
)
_ASSIGNMENT = re.compile(
    r"[\"']?(?i:api[_-]?key|access[_-]?token|refresh[_-]?token|password|token|secret|CLAUDE_CODE_OAUTH_TOKEN)[\"']?\s*[=:]\s*[A-Za-z0-9_-]{8,}"
)


def scrub_tokens(text: str) -> str:
    """Replace anything that looks like a credential in `text`."""
    return _SECRET.sub("[REDACTED]", text)


def scrub(value: Any) -> Any:
    """Return `value` with credentials redacted, recursing into lists and dicts."""
    if isinstance(value, str):
        return (
            "[REDACTED]"
            if _ASSIGNMENT.fullmatch(value)
            else _QUOTED_SECRET.sub("[REDACTED]", scrub_tokens(value))
        )
    if isinstance(value, dict):
        return {
            k: (
                scrub_tokens(v)
                if k in ("patch", "candidate_patch") and isinstance(v, str)
                else "[REDACTED]"
                if _SECRET_KEY.fullmatch(k)
                else scrub(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [scrub(v) for v in value]
    return value


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[str] = []
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("tr", "p"):
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("tr", "p") and self.parts:
            self.rows.append(" ".join(self.parts))
        if tag in ("td", "br", "p"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def report_rows(report: str) -> list[str]:
    """Return the text of each row of a run report, whether it is HTML or plain text."""
    if "<tr" not in report:
        return [html.unescape(line) for line in report.splitlines() if line.strip()]
    parser = _Rows()
    parser.feed(report)
    return [re.sub(r"\s+", " ", row).strip() for row in parser.rows]


def split_usage(text: str, report: str = "") -> tuple[str, dict[str, Any]]:
    """Split a trailing `factory-usage` comment off `text`; fall back to the last one in `report`.

    Returns `(text, usage)`, with `usage` empty when no comment is found.
    """
    match = re.search(r"\n<!--factory-usage (\{.*?\})-->\s*$", text, re.DOTALL)
    if match:
        return text[: match.start()], json.loads(match[1])
    matches = re.findall(r"\n<!--factory-usage (\{.*?\})-->", report, re.DOTALL)
    return text, json.loads(matches[-1]) if matches else {}


def price(
    usage: Mapping[str, Any] | None, model: str, prices: Mapping[str, list[float] | None] | None = None
) -> float | None:
    """Return the dollar cost of `usage` for `model`, or None when it is unknown or unpriced.

    A `claude:` model not in the table is priced at its family's rate. Rates are USD per million tokens.
    """
    if not usage or not any(field in usage for field in FIELDS):
        return None
    table = {**PRICES, **normalize_prices(prices)}
    model = normalize_key(model)
    rates = table.get(model)
    if model not in table and model.startswith("claude:"):
        family = re.match(r"claude:(?:claude-)?(opus|sonnet|haiku|fable)(?:[-\[]|$)", model)
        rates = table.get("claude:" + family[1]) if family else None
    if rates is None:
        return None
    return sum(float(usage.get(k, 0)) * rate / 1e6 for k, rate in zip(FIELDS, rates, strict=False))


def model_costs(
    models: Mapping[str, Mapping[str, Any]],
    engine: str,
    prices: Mapping[str, list[float] | None] | None = None,
) -> list[dict[str, Any]]:
    """Return `[{model, cost_usd}]` for the per-model token counts a task reported."""
    aliases = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheWriteInputTokens": "cache_creation_input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
    }
    return [
        {
            "model": name,
            "cost_usd": price(
                {aliases.get(k, k): v for k, v in counts.items()}, model_key(engine, name), prices
            ),
        }
        for name, counts in models.items()
    ]


def cost_breakdown(
    report: str, name: str, model: str = "sonnet", prices: Mapping[str, list[float] | None] | None = None
) -> dict[str, Any]:
    """Break the cost of a run report down by role, review round, fallback and delegate."""
    rows = report_rows(report)
    if "<tr" in report:
        rows.reverse()
    author = None
    roles: dict[str, dict[str, Any]] = {}
    rounds = []
    fallbacks = []
    gaps: list[dict[str, Any]] = []
    delegates = []
    delegate_jobs: dict[str, dict[str, int | None]] = {}
    review_counts = {}
    for row in rows:
        match = re.search(
            r"author session: ([\d.]+)M out, ([\d.]+)M in, ([\d.]+)M cache-write, ([\d.]+)M cache-read",
            row,
        )
        if match:
            out, inp, write, read = [float(v) * 1e6 for v in match.groups()]
            author = dict(zip(FIELDS, (inp, out, write, read), strict=False))
        if "run so far, sub-agents:" in row:
            text = row.split("run so far, sub-agents:", 1)[1].split("| author session:")[0]
            roles = {
                role: {
                    "tokens": float(tok) * 1e6,
                    "cost_usd": None,
                    "cli_cost_usd": float(cost),
                    "engine": role.rsplit("+", 1)[1] if "+" in role else None,
                    "tokens_by_type": None,
                }
                for role, tok, cost in re.findall(r"([\w+-]+) ([\d.]+)M tok \$([\d.]+)", text)
            }
        if "run so far, sub-agents:" in row:
            for role, payload in re.findall(
                r"([\w+-]+) [\d.]+M tok \$[\d.]+ \([^)]*\) \[usage (\{.*?\})\]", row
            ):
                basis = json.loads(payload)
                engines = basis.get("engines", [])
                models = basis.get("models", [])
                engine = engines[0] if len(engines) == 1 else None
                resolved = (
                    models[0] if len(models) == 1 else "default" if engine == "codex" and not models else None
                )
                roles[role].update(engine=engine, tokens_by_type=basis["tokens"], model=resolved)
                if engine and basis.get("modelUsage"):
                    parts = model_costs(basis["modelUsage"], engine, prices)
                    roles[role]["model_costs"] = parts
                    roles[role]["known_subtotal_usd"] = sum(
                        part["cost_usd"] for part in parts if part["cost_usd"] is not None
                    )
                    roles[role]["cost_usd"] = (
                        sum(part["cost_usd"] for part in parts)
                        if all(part["cost_usd"] is not None for part in parts)
                        else None
                    )
                elif engine and resolved:
                    roles[role]["cost_usd"] = price(basis["tokens"], model_key(engine, resolved), prices)
        if "round" in row.lower() and ("blocking" in row.lower() or "spend (sub-agents)" in row):
            rounds.append(row)
        if "🔀" in row:
            fallbacks.append({"row": row, "reset": row.split("reset", 1)[1] if "reset" in row else None})
        if "delegate" in row.lower():
            delegates.append(row)
            job = re.search(r"delegate (delegate-[\w-]+)", row)
            if job:
                record = delegate_jobs.setdefault(job[1], {"duration_s": None, "patch_files": None})
                duration = re.search(r"(?:finished in|failed after) (\d+)s", row)
                files = re.search(r"\((\d+) files\)", row)
                if duration:
                    record["duration_s"] = int(duration[1])
                if files:
                    record["patch_files"] = int(files[1])
        count = re.search(r"blocking findings, round (\d+): (\d+)", row)
        if count:
            review_counts[count[1]] = int(count[2])
        match = re.search(r"(?:idle for |reviewing \(|waiting[^\n]*?)([\d.]+)\s*(?:minutes?|mins?|m)\b", row)
        if match:
            gaps.append({"minutes": float(match[1]), "evidence": row})
    rates = {**PRICES, **normalize_prices(prices)}.get(normalize_key(model))
    # No context-size samples survive in the report. A fixed-size sensitivity
    # scenario is more honest than deriving a context size from cumulative tokens.
    cold = {
        "assumed_context_tokens": 100_000,
        "assumption": (
            "Each retained >5m observation causes one 100k-token cache write instead of a cache read; "
            "observations may overlap and are not measured misses."
        ),
        "incremental_cost_usd": (100_000 / 1e6 * (rates[2] - rates[3]) * sum(g["minutes"] > 5 for g in gaps))
        if rates
        else None,
    }
    # Status is replaced in-place, so only the largest retained idle observation is
    # countable; intervals between arbitrary timeline rows are not evidence of idle.
    return {
        "project": name,
        "coordinator": author,
        "coordinator_cost_usd": price(author, model, prices) if author else None,
        "assumed_author_model": model,
        "roles": roles,
        "review_round_evidence": rounds,
        "delegate_evidence": delegates,
        "delegate_jobs": delegate_jobs,
        "delegate_count": len(delegate_jobs) if delegate_jobs else None,
        "delegate_duration_s": sum(
            d["duration_s"] for d in delegate_jobs.values() if d["duration_s"] is not None
        )
        if delegate_jobs
        else None,
        "blocking_by_round": review_counts,
        "fallbacks": fallbacks,
        "fallback_cost_usd": None
        if any(v["cost_usd"] is None for k, v in roles.items() if "+" in k)
        else sum(v["cost_usd"] for k, v in roles.items() if "+" in k),
        "cold_reread_scenario": cold,
        "idle_gaps": gaps,
        "over_5m": sum(g["minutes"] > 5 for g in gaps),
        "over_60m": sum(g["minutes"] > 60 for g in gaps),
        "limitations": LIMITS,
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Return the records in a JSON-lines file, or none if it is missing.

    A record's `patch_file` is read into its `patch`.
    """
    if not Path(path).exists():
        return []
    with Path(path).open() as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    for record in records:
        if record.get("patch_file"):
            record["patch"] = (Path(path).parent / record["patch_file"]).read_bytes().decode("utf-8")
    return records


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append `record` to a JSON-lines file, scrubbing credentials from its patch."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    if isinstance(record.get("patch"), str):
        import hashlib

        original = record.pop("patch")
        patch = scrub_tokens(original)
        record["patch_scrubbed"] = record.get("patch_scrubbed", False) or patch != original
        patch_dir = path.parent / (path.stem + "-patches")
        patch_dir.mkdir(exist_ok=True)
        patch_path = patch_dir / (hashlib.sha256(patch.encode()).hexdigest() + ".patch")
        patch_path.write_bytes(patch.encode("utf-8"))
        record["patch_file"] = str(patch_path.relative_to(path.parent))
    with path.open("a") as stream:
        stream.write(json.dumps(scrub(record), ensure_ascii=False) + "\n")
        stream.flush()


def drop_jsonl(path: str | Path, predicate: Callable[[dict[str, Any]], bool]) -> None:
    """Remove rows matching `predicate`, so a retry replaces its failed row instead of duplicating it.

    Works on the raw lines: `read_jsonl` would inline stored patches into the rewritten file.
    """
    path = Path(path)
    if not path.exists():
        return
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    kept = [line for line in lines if not predicate(json.loads(line))]
    if len(kept) == len(lines):
        return
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text("".join(line + "\n" for line in kept))
    scratch.replace(path)


def save_json(path: str | Path, record: Any) -> None:
    """Write `record` to `path` as indented JSON with credentials redacted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(record), ensure_ascii=False, indent=2) + "\n")
