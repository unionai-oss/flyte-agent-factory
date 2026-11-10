"""Triage of `ask-human` questions: engineering judgement is decided here, the rest goes to the owner.

Projects escalated engineering calls that have a defensible right answer (design, structure, how to
satisfy a check, test strategy, tooling), often offering only bad options and a bad default: asked
whether to split an upstream file that broke the module-size rule, with "wait", "split upstream" and
"accept as-is" on offer, the right answer was a fourth -- put the new code in sibling modules. Another
asked the owner for waivers for checks failing on the gate's own environment; the owner's rule is "no
waivers, fix things" (factory issue #118).

So before a question reaches the owner a fresh-context judge classifies it. It decides what the right
thing to do is, against the owner's standing direction, the brief, the target repo's conventions and the
factory's standards -- not from the options the asker listed, and not swayed by the default it stated.
The decision is recorded with its reasoning and handed straight back to the asking session. A question in
an owner-reserved category, or one the judge is unsure of, still goes to the owner, with a recommendation.

`python -m flyte_agent_factory._judgement replay <fixture.json> ...` runs the same prompt through the
local `claude` CLI, to check the judge's calls against known questions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._reserved import (
    OWNER_RESERVED_FLOOR as OWNER_RESERVED_FLOOR,
)
from flyte_agent_factory._reserved import (
    STANDING_RULES as STANDING_RULES,
)
from flyte_agent_factory._reserved import (
    list_owner_reserved,
    list_standing_rules,
)

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from . import _timeline

#: What the decision becomes when the judge keeps proposing a waiver: the standing rule itself.
_NO_WAIVER_DECISION = (
    "Fix the failing check or the code so it passes on its merits; no waiver, lowered threshold, "
    "removed rule or skipped test."
)

#: The production judge's turn count; the bridge separately bounds how long the wait may take.
_JUDGE_MAX_TURNS = 40


@dataclass(frozen=True)
class Judgement:
    """The judge's call on one question. `klass` is "engineering" (decided here) or "owner"."""

    klass: str
    decision: str = ""
    reasoning: str = ""
    #: For a yes/no ask decided as engineering: True/False when the decision is a plain yes/no, else None.
    approve: bool | None = None
    recommendation: str = ""
    why_owner: str = ""
    #: For an engineering call: the judge's own declaration that the decision makes a check pass by getting
    #: round it (see `WAIVER` in the prompt). None when it did not say.
    waiver: bool | None = None


def judge_prompt(
    question: str,
    *,
    kind: str,
    brief: str,
    acceptance: str,
    directions: Sequence[str],
    owner_reserved: Sequence[str],
    standing: Sequence[str],
    standards: str,
) -> str:
    """Return what the judge is told. Modelled on `_review._arbiter_prompt`: the brief is the authority."""
    return (
        "You are the **judge** of questions a project session asks its owner (`ask-human`). Most of "
        "them are engineering judgement with a defensible right answer, and the owner does not want to "
        "be asked those: you decide them. The rest are the owner's to decide. You have a copy of the "
        "workspace: read the code and the target repo's conventions (its AGENTS.md, DESIGN.md, "
        "standards and checks) before you decide.\n\n"
        "## The authority\n\nThe brief, which neither you nor the asker can change:\n\n"
        f"{brief.strip()}\n\n"
        + (
            f"## The author's acceptance criteria (a proposal, not the authority)\n\n{acceptance.strip()}\n\n"
            if acceptance.strip()
            else ""
        )
        + (
            "## The owner's later direction\n\nGiven after the brief; where it narrows or changes the "
            "brief, it wins.\n\n" + "\n".join(f"- {d}" for d in directions) + "\n\n"
            if directions
            else ""
        )
        + "## The owner's standing rules\n\n"
        + "\n".join(f"- {rule}" for rule in standing)
        + "\n\n## Owner-reserved\n\nThese always go to the owner:\n\n"
        + "\n".join(f"- {category}" for category in owner_reserved)
        + (f"\n\n## The factory's standards\n\n{standards.strip()}" if standards.strip() else "")
        + f"\n\n## The question ({'a yes/no ask' if kind == 'approve' else 'free text'})\n\n"
        f"{question.strip()}\n\n"
        "## How to judge\n\n"
        "First classify. The question is OWNER if it falls in any owner-reserved category above, or if "
        "you are unsure; say UNSURE when you cannot tell. Otherwise it is ENGINEERING.\n\n"
        "For ENGINEERING, decide **what is the right thing to do**, judged against the owner's "
        "standing direction, the brief, the target repo's conventions and the factory's standards. "
        "You are **not limited to the options the asker listed**: they are often all bad, and the "
        "right answer is frequently one they did not think of. You are **not swayed by the default "
        "the asker stated**: a default chosen to cover silence is often wrong. Make the decision "
        "concrete enough to act on. Never choose a waiver, a lowered threshold, a removed rule or a "
        "skipped test: when a check is wrong, the decision is to fix the check.\n\n"
        "For OWNER or UNSURE, say which reserved category it falls in and what you would recommend "
        "the owner choose, and why.\n\n"
        "Answer in exactly this format, one field per line, field names as shown (a value may "
        "continue over following lines until the next field label):\n\n"
        "CLASS: ENGINEERING | OWNER | UNSURE\n"
        "DECISION: <what to do, concrete enough to act on>   (ENGINEERING)\n"
        "APPROVE: yes | no | neither   (ENGINEERING, only for a yes/no ask: yes or no only if the "
        "decision is a plain yes or no)\n"
        "WAIVER: yes | no   (ENGINEERING: yes if the decision makes a check pass by waiving it, moving a "
        "threshold or limit, exempting/excluding the project's own code from a rule, removing or disabling "
        "a rule or check, or skipping, deleting or weakening a test; no if it fixes a check that measures "
        "the wrong thing, such as one that walks third-party or vendored code the project does not own; "
        "be honest, a yes is not a failure)\n"
        "REASONING: <why this is the right thing to do>\n"
        "WHY OWNER: <which reserved category and why>   (OWNER/UNSURE)\n"
        "RECOMMENDATION: <what you would recommend the owner choose, and why>   (OWNER/UNSURE)"
    )


_FIELD = re.compile(
    r"^[ \t]*[*_]*(CLASS|DECISION|APPROVE|WAIVER|REASONING|WHY OWNER|RECOMMENDATION)"
    r"[*_]*[ \t]*:[*_]*[ \t]*(.*)$",
    re.IGNORECASE,
)


def _fields(text: str) -> dict[str, str]:
    """Return each labelled field's value; a value runs on until the next label. The first of a label wins."""
    out: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        match = _FIELD.match(line)
        if match:
            current = match.group(1).upper()
            if current in out:
                current = None
                continue
            out[current] = match.group(2)
        elif current is not None:
            out[current] += "\n" + line
    return {key: value.strip() for key, value in out.items()}


def parse(text: str) -> Judgement | None:
    """Return the judge's call, or None when it is not a complete one.

    None when it has no CLASS line, or decides ENGINEERING with no DECISION or no REASONING: a decision is
    recorded for the owner to read and overrule, which needs its reasons.
    """
    fields = _fields(text)
    klass = re.sub(r"[^A-Z]", "", fields.get("CLASS", "").upper())
    if klass == "ENGINEERING":
        decision = fields.get("DECISION", "")
        if not decision or not fields.get("REASONING", ""):
            return None
        approve = {"yes": True, "no": False}.get(fields.get("APPROVE", "").strip().lower().rstrip("."))
        waiver = {"yes": True, "no": False}.get(fields.get("WAIVER", "").strip().lower().rstrip("."))
        return Judgement("engineering", decision, fields.get("REASONING", ""), approve, waiver=waiver)
    if klass in ("OWNER", "UNSURE"):
        why = fields.get("WHY OWNER", "") or "owner-reserved"
        return Judgement(
            "owner",
            reasoning=fields.get("REASONING", ""),
            recommendation=fields.get("RECOMMENDATION", ""),
            why_owner=f"unsure: {why}" if klass == "UNSURE" else why,
        )
    return None


# The judge declares whether its decision is a waiver (`Judgement.waiver`); these phrases are the backstop for
# a declaration that is missing or wrong, so they lean towards flagging. The backstop is final: `decide` never
# records a decision they match, whatever the judge declares.
#
# A negation cancels a match only when it governs it: within a few words immediately before, in the same
# phrase. "Fix the gate rather than waiving the rule" refuses a waiver; "Rather than rewriting the module,
# add a waiver" does not. A bare "no"/"not" governs only the word right after it ("no waiver", "not waive"):
# "The gate has no fix so waive it" is a waiver.
# Multi-word negations are one unit when counting words, so "do not" is not cut in half by the reach.
_NEGATION = re.compile(
    r"\b(?:do(?:es)?[ _]not|don't|never|without|rather[ _]than|instead[ _]of|avoid(?:ing)?|"
    r"refus(?:e|es|ing)(?:[ _]to)?|reject(?:s|ing)?|forbid(?:s|den)?|"
    r"must[ _]not|should[ _]not|cannot|can't)\b",
    re.IGNORECASE,
)
_BARE_NEGATION = re.compile(r"\b(?:no|not)\b", re.IGNORECASE)
_NEGATION_REACH_WORDS = 3
# Limits of something the code does, not of a check: raising these is no waiver.
_OPERATIONAL = (
    r"(?:retr(?:y|ies)|timeouts?|rate|memory|cpu|connections?|pool|concurren\w*|batch|page|buffer|queue|"
    r"http|workers?|threads?)"
)
_FILLER = rf"(?:(?!{_OPERATIONAL}\b)\w+[ -]){{0,3}}?"
_NOT_DATA = r"(?! (?:fixtures?|files?|data|output|dirs?|directory|cache|artifacts?|snapshots?|logs?)\b)"
_CHECKISH = rf"(?:test_\w+|(?:tests?|checks?|gate)s?\b{_NOT_DATA})"
_WAIVER_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwaiv(?:e|ers?|ing|ed)\b",
        # Instructions only (lower, lowering), not "lowered threshold": the standing rule's own wording lists
        # "no waiver, lowered threshold, ...", and a passive is for the judge's declaration to catch.
        rf"\b(?:lower(?:ing)?|relax(?:ing)?|loosen(?:ing)?|raise|raising|bump(?:ing)?|"
        r"increas(?:e|ing)|set(?:ting)?) (?:the |its |a |each )?"
        rf"{_FILLER}(?:threshold|limit|ceiling|cap)s?\b",
        r"\b(?:lower(?:ing)?|raise|raising|bump(?:ing)?|increas(?:e|ing)) (?:the )?"
        rf"max[_-](?!{_OPERATIONAL})\w+",
        r"\b(?:remov(?:e|ing)|delet(?:e|ing)|drop(?:ping)?|disabl(?:e|ing)|turn(?:ing)? off|"
        r"switch(?:ing)? off)"
        rf" (?:the |this |that |a |an |each )?{_FILLER}(?:rules?|checks?|lint|gate|tests?\b{_NOT_DATA})s?\b",
        r"\b(?:skip(?:ping)?|xfail(?:ing)?|deselect(?:ing)?|ignor(?:e|ing)) (?:the |this |that |a |those )?"
        rf"{_FILLER}{_CHECKISH}",
        r"\bcomment(?:ing)? out (?:the |this |a )?(?:\w+[ -]){0,3}?(?:assert\w*|tests?|checks?|rules?)\b",
        r"\bas (?:skipped|xfail(?:ed)?)\b",
        r"\b(?:exclud\w*|exempt\w*|whitelist\w*|allowlist\w*)\b[^,;:\n]{0,60}?"
        r"\b(?:rules?|checks?|lint|gate|tests?|limits?|thresholds?)\b",
        r"\badd\w* [^,;:\n]{0,40}? to the [^,;:\n]{0,30}?(?:allowlist|whitelist|ignore[- ]list|exempt\w*)\b",
        r"\bdisabl\w+ (?:the )?[A-Z]{2,4}-\d{3}\b",
        r"\b(?:mov(?:e|ing)|chang(?:e|ing)|adjust(?:ing)?|rais(?:e|ing)) (?:the )?"
        rf"{_FILLER}(?:threshold|limit)s? to \d",
        r"\bgrandfather\w*\b",
        r"\bper-file-ignores\b",
        r"\bxfail\b|\bpytest\.mark\.(?:skip|xfail)",
        r"--deselect\b|--no-verify\b",
        r"\bnoqa\b|\btype:\s*ignore\b",
    )
)


def _governs(before: str) -> bool:
    """Whether a negation sits in the few words right before a match, within its own phrase."""
    phrase = re.split(r"[,:\u2014\u2013()]", before)[-1]
    words = _NEGATION.sub(lambda m: re.sub(r"\s+", "_", m.group()), phrase).split()
    return bool(
        _NEGATION.search(" ".join(words[-_NEGATION_REACH_WORDS:]))
        or _BARE_NEGATION.search(" ".join(words[-1:]))
    )


def waiver_phrase(text: str) -> str | None:
    """Return the phrase of a decision that chooses a waiver, a moved threshold or a skipped test, or None.

    A waiver here also covers a removed or bypassed rule or check.

    Negated mentions ("do not add a waiver", "rather than waiving", "without lowering the threshold") are
    the decision refusing one, so they do not count. Clause by clause: a negation in one sentence must not
    excuse a waiver proposed in the next.
    """
    for clause in re.split(r"[.;!?]+(?=\s|$)|\n+", text):
        for pattern in _WAIVER_PATTERNS:
            for match in pattern.finditer(clause):
                if not _governs(clause[: match.start()]):
                    return match.group(0)
    return None


def proposes_waiver(text: str) -> bool:
    """Whether `waiver_phrase` finds one."""
    return waiver_phrase(text) is not None


def _owner(why: str) -> Judgement:
    return Judgement("owner", why_owner=why)


async def decide(
    question: str, kind: str, call: Callable[[str], Awaitable[str]], context: Mapping[str, Any]
) -> Judgement:
    """Return the call on `question`: run the judge, parse, and refuse a waiver.

    `call(prompt)` runs the model. A judge that cannot be run or read means the owner is asked: the
    triage must never swallow a question. A decision is flagged when the judge declares it a waiver or the
    phrase guard matches it; it is rejected once with the reason and the judge decides again. If the second
    answer is still declared a waiver or still matches the guard, or the judge fails, the standing rule is
    the decision: the guard is final, whatever the judge declares.
    """
    ctx = {
        "brief": "",
        "acceptance": "",
        "directions": (),
        "standards": "",
        "owner_reserved": OWNER_RESERVED_FLOOR,
        "standing": STANDING_RULES,
        **context,
    }
    prompt = judge_prompt(question, kind=kind, **ctx)
    first = await _ask(call, prompt)
    if isinstance(first, str):
        return _owner(f"judge could not decide: {first}")
    phrase = waiver_phrase(first.decision) if first.klass == "engineering" else None
    if first.klass != "engineering" or (first.waiver is not True and phrase is None):
        return first
    flagged = (f"The phrase guard matched {phrase!r}. " if phrase else "") + (
        "You declared WAIVER: yes. " if first.waiver else ""
    )
    second = await _ask(
        call,
        prompt + "\n\n## Your earlier answer was rejected\n\n"
        f"You decided: {first.decision}\n\n{flagged}That chooses (or reads as) a waiver, a lowered "
        "threshold, "
        "a removed rule or a skipped test, which the owner's standing rules forbid. Decide again: when a "
        "check fails, fix the code or fix the check. State `WAIVER:` honestly. A second answer the guard "
        "still matches is replaced by the standing rule.",
    )
    if isinstance(second, Judgement):
        if second.klass != "engineering":
            return second
        again = waiver_phrase(second.decision)
        if second.waiver is not True and again is None:
            return second
        guard = f" the guard matched {again!r} in the second answer too;" if again else ""
    else:
        guard = ""
    return Judgement(
        "engineering",
        _NO_WAIVER_DECISION,
        "The judge's answer proposed a waiver, lowered threshold, removed rule or skipped test, which "
        f"the owner's standing rules forbid; it was rejected,{guard} the standing rule applies.",
    )


async def _ask(call: Callable[[str], Awaitable[str]], prompt: str) -> Judgement | str:
    """Run the judge once: its call, or why it could not be had."""
    try:
        text = await call(prompt)
    # What a failed sub-agent action, a refused route (`RouterDeferred` is a RuntimeError) or the local CLI
    # raises. Anything else is a bug, and the bridge's triage still sends the question to the owner.
    except (*REMOTE_ERRORS, subprocess.SubprocessError, asyncio.TimeoutError) as e:
        return f"{type(e).__name__}: {e}"
    return parse(text) or "its answer could not be read"


def _context() -> dict[str, Any]:
    """Return the judge's inputs, read as the review loop reads them; defaults without a profile."""
    from flyte_agent_factory import _inbox, _profile, _session_config, _standards

    profile = _profile.current()
    policy = profile.judgement if profile else None
    acceptance_path = _session_config._acceptance_path(_session_config.WORKSPACE)
    try:
        acceptance = acceptance_path.read_text()
    except OSError:
        acceptance = ""
    return {
        "brief": _session_config._project_brief,
        "acceptance": acceptance,
        "directions": list(_inbox._directions),
        "standards": _standards.review_text(_session_config._applicable_standards(), judgement_only=True),
        "owner_reserved": list_owner_reserved(policy),
        "standing": list_standing_rules(policy),
    }


async def run_judge(request: dict[str, Any], *, timeline: _timeline._Timeline) -> Judgement:
    """Judge `request` with a fresh-context sub-agent on a snapshot of the workspace, like the arbiter."""
    from flyte_agent_factory import _checkpoint, _policy, _reviewer, _session_config, _usage

    snapshots: list[Any] = []
    judge = _policy.resolve("judge")
    # The arbiter's effort: a wrong call here is one the owner never sees.
    effort = _policy.read("JUDGE_EFFORT", "").strip() or _policy.ARBITER_EFFORT

    async def call(prompt: str) -> str:
        # Uploaded inside the call, so a failed upload meets `_ask`'s boundary like a failed model call;
        # once, so the re-judge after a rejected waiver reads the same workspace.
        if not snapshots:
            snapshots.append(await _checkpoint._upload_workspace(_session_config.WORKSPACE))
        snapshot = snapshots[0]
        text, _ = await _reviewer._call_reviewer(
            timeline=timeline,
            purpose="judgement",
            round_=_usage._metering_round,
            answer_only=True,
            brief=prompt,
            workdir=snapshot,
            role="verify",
            model=judge.model,
            effort=effort,
            max_turns=_JUDGE_MAX_TURNS,
        )
        return str(text)

    return await decide(request.get("question", ""), request.get("kind", "text"), call, _context())


# --------------------------------------------------------------------------- #
# Replay: the same prompt through the local `claude` CLI, against known questions.
# --------------------------------------------------------------------------- #


def _claude_call(model: str) -> Callable[[str], Awaitable[str]]:
    async def call(prompt: str) -> str:
        cmd = ["claude", "-p", "--output-format", "text"] + (["--model", model] if model else [])
        # The local subscription CLI: no API key. A thread, so a slow call does not block other fixtures.
        # An empty directory: run where it was started, `claude` would read that checkout and its CLAUDE.md,
        # which are not the workspace the fixture describes.
        with tempfile.TemporaryDirectory() as empty:
            done = await asyncio.to_thread(
                subprocess.run,
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                cwd=empty,
            )
        if done.returncode != 0:
            raise RuntimeError(f"claude exited {done.returncode}: {done.stderr.strip()[:300]}")
        return done.stdout

    return call


def _delegate_call() -> Callable[[str], Awaitable[str]]:
    """Return a call to this pod's `delegate --answer` worker: a fresh Claude on the subscription, read-only.

    For a project pod, where the local `claude` is not signed in (the session's token is the supervisor's)
    and a sub-agent child cannot import this package (the pod's image does not carry it). The worker sees a
    snapshot of the pod's workspace, not the fixture's, so the fixture's context goes in the prompt.
    """

    async def call(prompt: str) -> str:
        def run(argv: list[str]) -> str:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=3600, check=False)
            if done.returncode != 0:
                raise RuntimeError(
                    f"{argv[0]} {argv[1]} exited {done.returncode}: {done.stderr.strip()[-300:]}"
                )
            return done.stdout

        submitted = await asyncio.to_thread(run, ["delegate", "--answer", prompt])
        job = submitted.strip().splitlines()[-1].strip() if submitted.strip() else ""
        if not job.startswith("delegate-"):
            raise RuntimeError(f"delegate printed no job id: {submitted.strip()[-300:]}")
        return await asyncio.to_thread(run, ["delegate", "wait", job])

    return call


async def _replay_one(fixture: dict[str, Any], model: str, engine: str = "claude") -> bool:
    extra = str(fixture.get("context") or "").strip()
    inner = _delegate_call() if engine == "delegate" else _claude_call(model)
    answers: list[str] = []

    async def call(prompt: str) -> str:
        shown = prompt + (f"\n\n## What the workspace shows\n\n{extra}" if extra else "")
        answers.append(await inner(shown))
        return answers[-1]

    context = {"brief": fixture.get("brief", ""), "directions": fixture.get("directions") or []}
    result = await decide(fixture["question"], fixture.get("kind", "text"), call, context)
    first = parse(answers[0]) if answers else None
    guard = (
        "rejected a waiver answer"
        if first and first.klass == "engineering" and (first.waiver or proposes_waiver(first.decision))
        else "clear"
    )
    expected = (fixture.get("expect") or {}).get("class")
    # A judge that never ran has decided nothing: its owner fallback is not a pass.
    ran = first is not None
    ok = ran and (expected is None or result.klass == expected)
    # A waiver the guard lets through is a failure of the guard; the standing rule it falls back to is not.
    ok = ok and not proposes_waiver(result.decision)
    # `no_waiver` fixtures must be decided without the guard having to step in at all.
    if (fixture.get("expect") or {}).get("no_waiver") and guard != "clear":
        ok = False
    print(f"== {fixture.get('name', '(unnamed)')}")
    print(f"class: {result.klass}")
    if result.klass == "engineering":
        print(f"decision: {result.decision}")
    else:
        print(f"why owner: {result.why_owner}")
    print(f"reasoning: {result.reasoning or '(none)'}")
    print(f"waiver guard: {guard}")
    print(
        f"{'PASS' if ok else 'FAIL'} (expected {expected or 'any'}{'' if ran else '; the judge did not run'})"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_judgement", description="Replay the judge against fixtures.")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="Run fixture files through the judge with the local claude CLI.")
    replay.add_argument("fixtures", nargs="+", type=Path)
    replay.add_argument("--model", default="", help="Model for `claude --model`; the CLI default if empty.")
    replay.add_argument(
        "--engine",
        choices=("claude", "delegate"),
        default="claude",
        help="claude: the local `claude -p`; delegate: this pod's `delegate --answer` worker.",
    )
    args = parser.parse_args(argv)

    async def run() -> list[bool]:
        results = []
        for path in args.fixtures:
            data = json.loads(path.read_text())
            for fixture in data if isinstance(data, list) else [data]:
                results.append(await _replay_one(fixture, args.model, args.engine))
        return results

    results = asyncio.run(run())
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
