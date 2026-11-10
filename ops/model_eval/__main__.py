"""Measure historical spend and compare fresh-context factory roles.

No model calls are made by costs, collect, or report. replay and grade launch
billable top-level tasks and enforce account budgets before each submission.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tarfile
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .data import FIELDS, cost_breakdown, read_jsonl, save_json, scrub
from .experiment import BACKEND_ERRORS, TOKEN_FIELDS, grade, reference, replay, settings, summarize


def config_at(path: str | None) -> dict[str, Any]:
    """Return the TOML settings at `path`, or an empty dict when no path is given."""
    if not path:
        return {}
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


def cases_at(path: str | Path) -> list[dict[str, Any]]:
    """Return the case records stored as `<dir>/*.json` under `path`."""
    return [json.loads(p.read_text()) for p in sorted(Path(path).glob("*/*.json"))]


def usd(value: float | None) -> str:
    """Format a dollar amount, or "unknown" when there is none."""
    return "unknown" if value is None else f"${value:.2f}"


def pct(value: float | None) -> str:
    """Format a fraction as a percentage, or "n/a" when there is none."""
    return "n/a" if value is None else f"{value:.0%}"


def secs(value: float | None) -> str:
    """Format a duration in seconds, or "n/a" when there is none."""
    return "n/a" if value is None else f"{value:.2f}"


def table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    """Return a markdown table, with the first column left-aligned and the rest right-aligned."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---:" if i else "---" for i, _ in enumerate(headers)) + "|",
    ]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n\n"


def tokens(counts: Mapping[str, int]) -> list[str]:
    """Return the formatted counts for each of `TOKEN_FIELDS`."""
    return [f"{counts[f]:,}" for f in TOKEN_FIELDS]


def report_tables(data: dict[str, Any]) -> str:
    """Return the evaluation report's markdown: problems first, then the result tables."""
    text = ""
    if data["errors"] or data["warnings"]:
        text += "## Problems\n\n" + "".join(f"- ERROR: {m}\n" for m in data["errors"])
        text += "".join(f"- WARNING: {m}\n" for m in data["warnings"]) + "\n"
    text += "## Candidates\n\n" + table(
        [
            "Purpose",
            "Setting",
            "n",
            "Graded",
            "Accuracy",
            "Mean $",
            "$ std dev",
            "Unknown $",
            "Mean s",
            "$ / correct",
        ]
        + ["Input tok", "Output tok", "Cache write tok", "Cache read tok"],
        [
            [
                g["purpose"],
                g["setting"],
                g["n"],
                g["graded"],
                pct(g["accuracy"]),
                usd(g["cost_usd"]["mean"]),
                usd(g["cost_usd"]["stdev"]),
                g["cost_usd"]["unknown"],
                secs(g["wall_s"]["mean"]),
                usd(g["cost_per_correct"]),
                *tokens(g["tokens"]),
            ]
            for g in data["groups"]
        ],
    )
    if data["ladders"]:
        text += "## Escalation ladders\n\n" + table(
            ["Purpose", "Order", "Cases", "Excluded", "Ladder $", "Fixed-setting $"],
            [
                [
                    ladder["purpose"],
                    " > ".join(ladder["order"]),
                    len(ladder["cases"]),
                    len(ladder["excluded"]),
                    usd(ladder["cost_usd"]),
                    usd(ladder["fixed_cost_usd"]),
                ]
                for ladder in data["ladders"]
            ],
        )
    spend = data["evaluation_spend"]
    text += (
        "## Evaluation spend\n\nPanel, adjudicator and grader calls; not part of any candidate's cost.\n\n"
    )
    text += table(
        [
            "Role",
            "Calls",
            "Cost",
            "Unknown $",
            "Input tok",
            "Output tok",
            "Cache write tok",
            "Cache read tok",
        ],
        [
            [role, s["calls"], usd(s["cost_usd"]), s["unknown_cost"], *tokens(s["tokens"])]
            for role, s in spend.items()
        ],
    )
    text += (
        "## Agreement by family\n\nCandidates are graded by the other family; adjudication alternates per "
        "case.\n\n"
    )
    text += table(
        ["Candidate family", "Adjudicator family", "n", "Verdict agreement"],
        [
            [a["candidate_family"], a["adjudicator_family"], a["n"], pct(a["agreement"])]
            for a in data["family_agreement"]
        ],
    )
    text += "## Grader bias\n\nRows graded by both families: same-family minus other-family correctness.\n\n"
    text += table(
        ["Candidate family", "n", "Other-family correct", "Same-family correct", "Bias", "Disagreements"],
        [
            [
                b["candidate_family"],
                b["n"],
                pct(b["other_family_correct"]),
                pct(b["same_family_correct"]),
                f"{b['bias']:+.0%}",
                b["disagreements"],
            ]
            for b in data["grader_bias"]
        ],
    )
    failures = data["failures"]
    text += (
        f"## Failures\n\nGrade attempts failing now: {failures['grade_errors']}. "
        f"Skipped grades: {len(failures['grade_skipped'])}. Skipped references: "
        f"{len(failures['reference_skipped'])}.\n\n"
    )
    return text


def emit(data: dict[str, Any], output: str, title: str) -> None:
    """Write `data` to `<output>.json` and its markdown rendering to `<output>.md`."""
    save_json(f"{output}.json", data)
    text = f"# {title}\n\n"
    if "projects" in data:
        text += (
            "| Project | Coordinator $ | Sub-agent $ | Idle >5m | Idle >60m |\n|---|---:|---:|---:|---:|\n"
        )
        for project in data["projects"]:
            spend = project["coordinator_cost_usd"]
            shown = usd(spend)
            child = sum(
                role["cost_usd"] for role in project["roles"].values() if role["cost_usd"] is not None
            )
            child_shown = (
                "unknown"
                if any(role["cost_usd"] is None for role in project["roles"].values())
                else usd(child)
            )
            text += (
                f"| {project['project']} | {shown} | {child_shown} | {project['over_5m']} | "
                f"{project['over_60m']} |\n"
            )
    if "groups" in data:
        text += report_tables(data)
    text += (
        f"\nComplete evidence and limitations:\n\n```json\n"
        f"{json.dumps(scrub(data), indent=2, ensure_ascii=False)}\n```\n"
    )
    Path(f"{output}.md").write_text(text)
    print(f"{output}.md\n{output}.json")


async def execute(args: argparse.Namespace) -> None:
    """Run the subcommand in `args` (costs, collect, replay, grade or report) and write its output."""
    config = config_at(args.config)
    config["local_mechanical"] = getattr(args, "local_mechanical", False)
    if args.command == "report":
        data = summarize(read_jsonl(args.results), read_jsonl(args.grades), args.ladder, cases_at(args.cases))
        emit(data, args.output, "Model evaluation")
        for message in data["errors"] + data["warnings"]:
            print(message, file=sys.stderr)
        if data["errors"]:
            # The report is written, but an empty ladder must not look like a finished one.
            raise SystemExit(2)
        return
    if args.command == "costs" and args.report_file:
        projects = [
            cost_breakdown(Path(p).read_text(), Path(p).stem, args.author_model, config.get("prices"))
            for p in args.report_file
        ]
    else:
        from .remote import Backend, collect_run, discover_models

        backend = Backend(config, getattr(args, "allow_live", False))
        if args.command in ("costs", "collect"):
            runs = backend.runs(args.runs, args.days)
            if args.command == "collect":
                for run in runs:
                    paths = await collect_run(run.name, args.cases, backend.remote)
                    print(f"{run.name}: {len(paths)} cases")
                return
            projects = [
                cost_breakdown(str(r.get_report()), r.name, args.author_model, config.get("prices"))
                for r in runs
            ]
        elif args.command == "replay":
            variants = settings(config, await discover_models(config))
            cases = cases_at(args.cases)
            if args.reference:
                try:
                    await reference(
                        cases,
                        backend,
                        config,
                        args.allow_live,
                        persist=lambda case: save_json(Path(args.cases) / f"{case['id']}.json", case),
                    )
                finally:
                    for case in cases:
                        save_json(Path(args.cases) / f"{case['id']}.json", case)
            else:
                await replay(cases, variants, backend, args.results, config, args.allow_live)
            return
        else:
            await grade(
                cases_at(args.cases), read_jsonl(args.results), backend, args.grades, config, args.allow_live
            )
            return
    known = [p for p in projects if p["coordinator"] is not None]
    costs = [p["coordinator_cost_usd"] for p in projects if p["coordinator_cost_usd"] is not None]
    totals = {
        "coordinator": {f: sum(p["coordinator"][f] for p in known) for f in FIELDS},
        "coordinator_cost_usd_known": sum(costs),
        "projects_with_known_tokens": len(known),
        "projects_with_known_cost": len(costs),
        "projects": len(projects),
        "subagent_cost_usd_known": sum(
            v["cost_usd"] for p in projects for v in p["roles"].values() if v["cost_usd"] is not None
        ),
    }
    emit({"projects": projects, "totals": totals}, args.output, "Factory cost breakdown")


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the chosen model-evaluation subcommand."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="TOML settings, prices and budget thresholds")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("costs", "collect", "replay", "grade", "report"):
        sub = commands.add_parser(command)
        if command in ("costs", "collect"):
            sub.add_argument("runs", nargs="*")
            sub.add_argument("--days", type=int)
        if command in ("collect", "replay", "grade", "report"):
            sub.add_argument("--cases", default="ops/model_eval/cases")
        if command in ("replay", "grade"):
            sub.add_argument("--allow-live", action="store_true")
        if command in ("replay", "grade", "report"):
            sub.add_argument("--results", default="ops/model_eval/results.jsonl")
        if command in ("grade", "report"):
            sub.add_argument("--grades", default="ops/model_eval/grades.jsonl")
        if command in ("costs", "report"):
            sub.add_argument("--output", default=f"ops/model_eval/{command}")
        if command == "costs":
            sub.add_argument("--report-file", action="append", default=[])
            sub.add_argument("--author-model", default="sonnet")
        if command == "grade":
            sub.add_argument(
                "--local-mechanical",
                action="store_true",
                help="unsafe for untrusted patches: run checks locally",
            )
        if command == "replay":
            sub.add_argument("--reference", action="store_true")
        if command == "report":
            sub.add_argument("--ladder", nargs="*", default=[])
    args = parser.parse_args(argv)
    try:
        asyncio.run(execute(args))
    except (*BACKEND_ERRORS, tarfile.TarError) as exc:  # a traceback would expose serialized SDK credentials
        # SDK errors can embed serialized task inputs and credentials outside known token formats.
        detail = ""
        if type(exc).__module__ == "builtins":
            message = str(exc)
            import re

            message = re.sub(r"(?i)(?:secret|token|password|api_key)=[^\s]+", "[REDACTED]", message)
            detail = f": {scrub(message)}"
        parser.exit(
            1,
            f"model_eval stopped ({type(exc).__name__}{detail}); inspect saved records; no further "
            f"launches\n",
        )


if __name__ == "__main__":
    main()
