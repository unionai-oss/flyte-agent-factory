"""The text the session and its tools are given: project memory, guidance and the notes it embeds."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from flyte_agent_factory import (
    _context_guard,
    _gate_base,
    _issues,
    _meter,
    _session_config,
    _standards,
    _target_ci,
)
from flyte_agent_factory._reserved import list_owner_reserved
from flyte_agent_factory.browser import personal_guidance

#: The paths no `May edit:` line can grant; the gate's scope check blocks them whatever the brief says.
_SCOPE_NEVER_EDIT = (
    "`standards/`, `waivers.toml` and `gardening.toml` are the owner's: never edit them, "
    "whatever `May edit:` says. "
)


def _push_command(branch: str, default_branch: str) -> str:
    """Return the line the session is told to run to bring in the remote and push.

    On a project branch this merges and never rebases or forces: a rebase rewrites pushed commits
    and then needs a force-push, which can discard work a human or an earlier pod pushed to the
    branch. The push names `refs/heads/<branch>` explicitly, so it can write nothing else, even
    from a detached HEAD or from the default branch. `--prune` drops `origin/<branch>` once the
    branch is deleted on the remote (a merged PR), so the push then recreates it.
    """
    if branch:
        return (
            f"git fetch --prune origin && (! git rev-parse -q --verify origin/{branch} >/dev/null "
            f"|| git merge --no-edit origin/{branch}) && git merge --no-edit origin/{default_branch} "
            f"&& git push -u origin HEAD:refs/heads/{branch}"
        )
    return "git fetch origin && git rebase origin/main && git push origin HEAD"


def _write_credential_guidance() -> str:
    """Return the `credential-grant` section of the main session's memory (sub-agents lack the command)."""
    return (
        "- **Credentials the owner grants you** (`credential-grant`, `docs/credential-grants.md`). You hold "
        "no cloud credential. When the work needs AWS access, ask for the shortest time that does the "
        'job: `credential-grant request <name> --hours N --why "<what for>"` (a lower case name such as '
        "`aws-marketplace`). It asks the owner and waits; exit 0 means it is granted. Use it only through "
        "`credential-grant exec <name> -- <command>` (in this pod, by you and the processes you start; "
        "child actions and delegate workers cannot use it). `credential-grant list` shows what you hold "
        "and until when; `credential-grant revoke <name>` gives it back: do that when the job is done. "
        "You are told when one ends. **Never print, echo or write the credential, its values, or the "
        "environment of an `exec` to the terminal, a file, a commit, a note or a question**, and never "
        "ask the owner to type one into chat.\n"
    )


def _asking_guidance() -> str:
    """When to ask a human.

    The attended version used to say "asking costs nothing. Prefer it over
    guessing", and sessions followed it: every run came back to a person for
    direction it could have found in five minutes of reading. Asking is cheap
    for the *session* and expensive for the human, so the default posture is
    resolve-and-proceed, and a question that must be asked carries a timeout
    and a default so that no unanswered question can stop the work.
    """
    # The profile's owner_reserved extras must reach the session too: it classifies a question before
    # the judge ever sees it.
    policy = _session_config._PROFILE.judgement if _session_config._PROFILE else None
    categories = (
        *list_owner_reserved(policy),
        "anything outside your remit (another project's files)",
        "a genuine contradiction in the brief that no reading resolves",
    )
    blocking = (
        "- **Block on a human only for owner-reserved questions:** "
        + "; ".join(categories)
        + ". Nobody is watching this run, so ask one at a time, plainly, with your recommendation, a "
        "timeout and the most conservative default (the one that can be undone). Timeouts and defaults "
        "are for these questions only.\n"
    )
    return (
        "- **Resolve, then proceed; asking is the last resort, not the first move.** Handing back "
        "is not a safe default, it is an unfinished run. Before you ask anything, answer it "
        "yourself: read the code, the git history and the brief again, or run an experiment. "
        "Most ambiguities are answerable in five minutes of looking.\n"
        "- **Classify the question before you ask it:** owner-reserved (listed below) or engineering "
        "judgement (design, structure, refactoring, how to satisfy a check, test strategy, tooling). "
        'Engineering judgement is yours to decide, by asking "what is the right thing to do?", '
        "weighing options beyond the ones in front of you. Record the decision and your reasoning "
        'with `flyte-note "DECISION: ..."` and carry on. Never ask for, or choose, a waiver, a '
        "lowered threshold, a removed rule or a skipped test to get a check to pass: fix the code or "
        "the check. If you ask an engineering question anyway, the factory's judge decides it, not "
        "the owner, and you act on its answer. Owner direction that overrules a factory decision "
        "wins; act on it.\n"
        "- **Ask an owner-reserved question with a timeout and a stated default:** "
        '`ask-human --timeout <seconds> "<question> Default if no answer: <what you will do>"` '
        "(`--approve` for a yes/no). When it times out, do the default, record the decision and "
        "your reasoning in a `flyte-note`, and carry on. Never let an unanswered question stop "
        "the work.\n"
        "- **Close questions that stop being live.** A reply typed into this session (the Claude "
        "app) closes the open questions by itself. If a question is answered another way (a "
        "direct- message) or is no longer needed, run "
        '`ask-human --withdraw <id> --reason "<why>"` (the id is printed when you ask); when '
        "you re-ask, pass `--supersedes <id>` so the old one is closed.\n" + blocking
    )


def spend_policy_block() -> str:
    """State the standing rule on spending, so a session does not ask about every cheap call."""
    return (
        "## Spend\n\n"
        "- **Subscription use is not spend: use it freely, no ask.** Prefer the cheapest model that "
        "does the job, Haiku first for bulk work such as replay, labelling and triage (hundreds of "
        "calls are fine). The only bound is the account reserve, which the factory enforces: do not "
        "ration yourself below it.\n"
        "- **Ask first, with `ask-human`, before** any API-key call, any paid `factory-bench` run, or "
        "any cloud resource above the profile threshold.\n\n"
    )


def _window_text(label: str, pct: float | None, resets_at: float | None) -> str:
    resets = (
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(resets_at)) if resets_at is not None else "unknown"
    )
    return f"{label} window {pct:g}% used, resets {resets}"


def headroom_block(meters: Iterable[_meter.Meter]) -> str:
    """Describe what each account has left, per window it reported; unreported windows are not listed."""
    lines = []
    for meter in sorted(meters, key=lambda m: m.account):
        windows = [
            _window_text(label, pct, resets_at)
            for label, pct, resets_at in (
                ("5-hour", meter.five_hour_pct, meter.five_hour_resets_at),
                ("weekly", meter.weekly_pct, meter.weekly_resets_at),
            )
            if pct is not None
        ]
        windows += [
            _window_text(f"{s.scope} weekly", s.pct, s.resets_at) for s in meter.scoped if s.pct is not None
        ]
        if windows:
            lines.append(f"- {meter.account}: " + "; ".join(windows))
    return (
        "### Account headroom\n\n"
        + ("\n".join(lines) if lines else "- no usage reading yet; assume nothing about the windows")
        + "\n"
    )


HEADROOM_BEGIN = "<!-- flyte-agent-factory: headroom begin -->"
HEADROOM_END = "<!-- flyte-agent-factory: headroom end -->"


def headroom_section(meters: Iterable[_meter.Meter]) -> str:
    """`headroom_block` between markers, so `refresh_headroom` can replace it inside the managed block."""
    return f"{HEADROOM_BEGIN}\n{headroom_block(meters)}{HEADROOM_END}\n\n"


def refresh_headroom(meters: Iterable[_meter.Meter]) -> list[Path]:
    """Rewrite the headroom section of the session's memory file(s); return those that changed.

    The meter loop calls this on every refresh. A reading that changes only its timestamp is not a
    change, so the file is not rewritten (and the session's file watcher not woken) for nothing.
    """
    block = f"{HEADROOM_BEGIN}\n{headroom_block(meters)}{HEADROOM_END}"
    changed = []
    for path in (_session_config.WORKSPACE / "CLAUDE.md", _session_config.CODEX_HOME_DIR / "AGENTS.md"):
        try:
            text = path.read_text()
            before, begin, rest = text.partition(HEADROOM_BEGIN)
            _, end, after = rest.partition(HEADROOM_END)
            if not (begin and end) or f"{before}{block}{after}" == text:
                continue
            path.write_text(f"{before}{block}{after}")
        except OSError:
            continue
        changed.append(path)
    return changed


def _coordinator_note() -> str:
    return (
        "## Implementation coordinator\n\n"
        "You are the coordinator: keep decomposition, precise briefs, decisions, judging patches, "
        "talking to the human, and small surgical or integration edits. Hand everything else to "
        "`delegate`, as self-contained chunks (one feature or fix list) with exact files, "
        "acceptance criteria and test commands: implementation, exploring unfamiliar code or docs, "
        "long test runs and failure triage, bulk edits, browser research, writing tests -- work "
        'whose answer is cheaper to check than to produce. Use `delegate --answer "<question>"` '
        "for read-only research. You will be told when a delegation finishes, so you may end "
        "your turn while waiting. Run independent chunks in parallel (`delegate status`, "
        "`delegate wait <id>`) rather than a sequence of your own work, and read worker summaries "
        "and diffs instead of re-deriving them. Workers see only a snapshot, never this "
        "conversation, so each brief must stand alone. You are accountable for every patch: "
        "review it critically, commit your own local changes first, then "
        "`git apply --3way <patch-path>`, run the checks, and commit. A worker that runs out of "
        "turns ends `partial`: `delegate wait` prints its summary, a partial patch and a `plan.md` of "
        "what remains. Apply the partial patch the same way, then `delegate` the plan (as a "
        "self-contained brief) rather than starting over.\n\n"
    )


def _context_hygiene_note(delegation_available: bool) -> str:
    # `delegate --answer` exists only where delegation does; elsewhere a sub-agent is the place for it.
    explore = "`delegate --answer` or a sub-agent" if delegation_available else "a sub-agent"
    return (
        "## Context hygiene\n\n"
        "Every turn re-sends the whole context, so each token you read is paid for again on every later "
        "turn. Read large files by line range (`sed -n 'a,bp'`, `grep -n`, Read with offset and limit), "
        "not whole. Keep command output short (`-q`, `| tail -n 40`). Send exploration of unfamiliar code "
        f"to {explore} and read its conclusion. Edits, debugging and review "
        "still use the exact source.\n\n"
    )


def _project_memory(
    browser_enabled: bool,
    *,
    console_token: Path | None = None,
    name: str = "demo",
    package: Path | None = None,
    voice_model: Path | None = None,
    delegation_available: bool = False,
    layout: str = "sandbox",
    branch: str = "",
    default_branch: str = "main",
    branch_problem: str = "",
    engine: str = "claude",
    personal_browsers: Sequence[str] = (),
) -> str:
    root = layout == "root"
    workspace = layout == "workspace"
    # With the context guard on, the ~16 KB demo section (and the console block it repeats) is left out of a
    # brief that does not ask for a demo; off, the text is exactly what it always was.
    guard = _context_guard.enabled()
    recording_wanted = (
        not guard
        or bool(_session_config._demo_kind)
        or _context_guard.wants_demo(_session_config._project_brief)
    )
    push_command = _push_command(branch, default_branch)
    branch_note = (
        f"- **You work on the branch `{branch}`, not on `{default_branch}`.** Commit and push there and "
        f"never push to `{default_branch}`. When you declare DONE the supervisor opens a draft pull "
        "request from this branch (you cannot and need not); when review passes it is marked ready "
        "for review. Do not open, merge or close pull requests yourself.\n"
        + (f"- **Start here: {branch_problem}**\n" if branch_problem else "")
        if branch
        else ""
    )
    codex_note = (
        "**You are a Codex session, and Codex has no subagent roles here.** There is no "
        "`.claude/agents/` for you: the roles `scope`, `discover`, `prototype`, `shape`, `build` and "
        "`verify` do not exist as threads of your process, and there is no `PushNotification` tool (where "
        "a message asks for one, use `ask-human`). "
        + (
            "Get what they give by `delegate`: a worker per role-sized chunk, with the role's job "
            'written into its brief, and `delegate --answer "<question>"` for read-only review. '
            "Run **several reviewers at once** on different aspects. "
            if delegation_available
            else "This project has no repository to delegate from, so plan and review yourself. "
        )
    )
    roles_note = (
        "The roles, each with when to use it and the file it writes: "
        "`scope` (start of a project, or the brief is ambiguous or drifting) writes the ACCEPTANCE.md "
        "criteria draft; `discover` (a problem-brief issue, or a design question the code cannot answer) "
        "writes DISCOVERY.md; `prototype` (a choice between shapes a person will see or use) writes "
        "prototypes/ with at least two runnable options and an evaluation; `shape` (before building, "
        "or when a design fights the problem) writes DESIGN-RATIONALE.md; `build` (the shape is settled) "
        "writes code and tests; `verify` (after any build) gives evidence-cited verdicts. Each reads "
        "what the stage before it wrote, except `verify`, which reads only the criteria and the "
        "artifact. For a problem-brief issue -- the brief says the design is to be produced -- the order "
        "is `scope`, `discover`, `prototype` (when a person will see or use the result), `shape`, "
        "`build`, `verify`. The old names `product`, `designer`, `dev` and `qa` still work as aliases "
        "this release. "
    )
    claude_team_note = (
        "**Use your own subagents for planning and review.** Implementation goes through `delegate` "
        "by default; the roles in `.claude/agents/` share this container and workspace. "
        + roles_note
        + "Run **several `verify` at once** on different aspects. "
        if delegation_available
        else "**Your own subagents first.** The roles in `.claude/agents/` are threads of this "
        "process, sharing this container and workspace. Right for reading, reviewing and parallel "
        "edits, and free. " + roles_note + "Run **several `verify` at once** on different aspects. "
    )
    team_note = codex_note if engine == "codex" else claude_team_note
    browser_note = (
        "- **A real Chromium runs beside you**, as the `browser` MCP server. Use it for anything "
        "needing a live page: checking that a UI you built actually looks right, reading a "
        "JS-rendered site, reproducing a visual bug. Its profile is persistent, so a human may "
        "have already signed in to things -- check before assuming you need credentials, and "
        "never try to log in to something yourself.\n"
        "- **Check which browser you have; the report says.** Normally it runs *in this pod*, "
        "which means **`http://localhost:<port>` works**: serve a page -- a report, a plot, a dev "
        "server -- and navigate straight to it. That is much better than encoding a file into a "
        "URL, so reach for it first.\n"
        "- **If instead you were given the shared browser app**, it is in a different pod: it "
        "cannot see your localhost and has no git credentials, so a localhost URL or a private "
        "repo link will fail. Show it content by writing the HTML and navigating to "
        "`data:text/html;base64,<base64 of the file>`. It is also a single seat shared by every "
        "project, so if a call returns `Browser is already in use for ...` another project holds "
        "it -- that error comes back on *every* call, including `browser_start_video`. Do not "
        "retry in a loop: say so with `flyte-note` and do work that does not need it.\n"
        # With the guard on the console block appears once: here, unless the demo section below carries it.
        f"{_console_auth_note(console_token) if console_token and not (guard and recording_wanted) else ''}"
        "- **It can record.** `browser_start_video` / `browser_stop_video` capture what the "
        "browser does, `browser_video_chapter` drops a titled chapter card, and "
        "`browser_video_show_actions` / `browser_video_hide_actions` toggle the click overlay -- "
        "so a walkthrough can be a watchable artifact rather than a screenshot dump. "
        "`browser_take_screenshot` and `browser_pdf_save` are there too.\n"
        "- **Recordings do not come back on their own.** They land in the browser container, "
        "which is replaced on every redeploy. `browser_stop_video` returns the filename; list "
        "`<browser-endpoint>/output/` (JSON) and `curl` the `.webm` into your workspace so it is "
        "published with the run. It is served as `text/plain` -- save the bytes, do not try to "
        "interpret the content type. A recording you did not fetch is a recording you lost.\n"
        + (
            "- **If the brief asks for a demo**, read *Recording a demo of this work* at the end of "
            "this file before you plan it. The rules there are not stylistic: two shipped demos had "
            "to be withdrawn for breaking them.\n"
            if recording_wanted
            else ""
        )
        if browser_enabled
        else ""
    )
    api_key_note = (
        (
            "### The Anthropic API key\n\n"
            f"This pod has one, in `${_session_config._PROJECT_API_KEY_VAR}`.\n\n"
            "**It is deliberately not called `ANTHROPIC_API_KEY`.** Claude Code reads that name "
            "and bills the API with it, which spends real credit on a session the subscription "
            "already pays for -- and a session billing the API reports itself logged out. "
            "Renaming it is what keeps the two apart.\n\n"
            "So it is in reserve for *the project's own* calls to Anthropic -- a benchmark, a "
            "client library, a script that must reach the API directly -- and nothing else. Set "
            "it only for the one process that needs it:\n\n"
            "```sh\n"
            f"ANTHROPIC_API_KEY=${_session_config._PROJECT_API_KEY_VAR} python bench.py\n"
            "```\n\n"
            "Never `export` it into your own shell, never write it to a file, never commit it "
            "and never print it. If your brief does not involve calling Anthropic directly, you "
            "do not need it at all -- and most briefs do not.\n\n"
        )
        if os.environ.get(_session_config._PROJECT_API_KEY_VAR)
        else ""
    )
    recording_note = (
        _recording_note(
            name=name,
            package=package
            if package is not None
            else _session_config.WORKSPACE / _session_config.DEMO_SUBDIR / name,
            console_token=console_token,
            voice_model=voice_model,
        )
        if browser_enabled and recording_wanted
        else ""
    )
    acceptance = _session_config._acceptance_rel(name)
    checks = _session_config._checks_rel(name, layout)
    project_rel = _gate_base.project_rel(name, layout)
    delivery_note = (
        "- **This project delivers to another repo, so two criteria are always on that list:** "
        '"target repo CI green locally (the commands `factory:target-ci` runs)" and "100% branch '
        "coverage of changed lines with the target's coverage command, or each uncovered line listed "
        'with a reason in SUMMARY.md". The supervisor runs both before any reviewer. List an '
        "uncovered line under a `## Uncovered changed lines` heading in SUMMARY.md as "
        "`path:line -- reason`.\n"
        f"- **The target's files are in `{project_rel}/<path>`, as `{project_rel}/promote.toml` says "
        "(`path`, `.` for the project directory itself).** Edit and commit them there, in this workspace: "
        "they are what gets reviewed and delivered. Never clone the target inside the project. "
        "`factory:target-ci` and the coverage check run on a copy of that tree built from this workspace's "
        "history, against the commit it was imported from. If `promote.toml` is missing, the gate fails "
        "naming `python ops/promote.py import <target> <project directory>`, which only the operator runs: "
        "ask for it rather than writing the file yourself. Delivering is the operator's step too, and "
        "only committed work is delivered.\n"
        if _target_ci.parse_target(_session_config._project_brief)
        else ""
    )
    return (
        _standards.GUIDANCE
        + (_issues.GUIDANCE + "\n\n" if _issues.is_framed(_session_config._project_brief) else "")
        + (_coordinator_note() if delegation_available else "")
        + (_context_hygiene_note(delegation_available) if guard else "")
        + spend_policy_block()
        + headroom_section([])
        + "## You are running on Flyte\n\n"
        "This session is a long-lived Flyte run on Union. It is expected to last days or months, "
        "across pod restarts. Some notes that follow from that:\n\n"
        f"{_asking_guidance()}"
        f"{personal_guidance(personal_browsers)}"
        f"{_write_credential_guidance()}"
        "- **See CI for your branch with `ci-status`** -- you hold no GitHub credentials, and this is "
        "read-only. It lists the GitHub Actions runs, check-runs and statuses of your branch head "
        "(`--sha <sha>` for another commit, `--json` for machine output); `ci-status --logs <job-id>` "
        "prints the end of one job's log, where the id is the one `ci-status` shows beside a check-run. "
        "Answers are rate-limited to one GitHub call per 10 seconds, so push, wait for the run, then "
        "ask once rather than polling. If it says the App lacks a permission, say so with a "
        "`flyte-note` and carry on without that part.\n"
        '- **Report progress** with `flyte-note "<what just happened>"`. It lands in the run '
        "report, which is where anyone checking on you will look. The run shows `RUNNING` whether "
        "you are mid-training or finished an hour ago, so the report is the only way anyone knows "
        "which.\n"
        "### Finishing is a gate, not a decision\n\n"
        f"- **Write `{acceptance}` before you build anything** -- "
        + (
            "in the ignored harness directory, never as a file at the repository root. "
            if root or workspace
            else "in this project's own directory, "
            "never the repository root, which other projects in this shared repo also use. "
        )
        + "Turn the brief into "
        "concrete, checkable criteria -- each one something a reviewer could run and get a yes "
        'or a no from. "Error handling is good" is not a criterion; "`POST /x` with a '
        'malformed body returns 400 and logs nothing at ERROR" is. Twenty-odd is normal for a '
        "real brief. Keep it in the ignored harness directory; it is checkpointed. You are going to be "
        "judged against the brief whatever you write "
        "here, so writing it narrow buys you nothing.\n"
        f"{delivery_note}"
        "- **A copy of another repository kept in the project directory (no `.git`) is declared in "
        f"`{project_rel}/vendored.toml`**: one `[[tree]]` with `path`, `repo` (https) and `commit` (full "
        "sha) per copy. The gates then judge only the files you changed in it (module size, test removal, "
        "language detection); no waiver is needed. See `docs/standards.md` in the engine.\n"
        '- **Say when you are done** with `flyte-note "DONE: <one line on what was delivered and '
        'where>"`. That does not end the work -- it starts a review. An independent reviewer, in '
        "its own container with its own copy of this workspace and no knowledge of your "
        "reasoning, grades your work against the brief and runs what you built. You do not pick "
        "it, you do not see it coming, and you cannot talk to it. Before any reviewer starts, "
        f"the supervisor checks the mechanical facts -- `{acceptance}` exists and is "
        "nonempty, nothing tracked is uncommitted, nothing is unpushed -- and bounces a DONE "
        "that fails them straight back with the commands to run. That costs no review round.\n"
        f"- **Declare your checks in `{checks}`.** Write a bash script with the commands "
        "that prove the work builds and its tests pass, run from the workspace root, exiting 0 "
        "only when everything passes; paths inside it are relative to the repository root. "
        "Commit the script"
        + (" with `git add -f`, because `.claude-flyte/` is git-ignored in this clone" if root else "")
        + ". The supervisor runs it before any reviewer; a failure "
        "bounces DONE back with the output log path without using a review round. "
        "Checks run against the working tree including untracked files, while reviewers see only "
        "what is committed, so commit what the checks depend on.\n"
        f"- **If it rejects you**, the findings arrive as a message and you fix them and declare "
        f"DONE again, with a plan for the next round written from every finding. You get "
        f"{_session_config._QA_MAX_ROUNDS} rounds at most, while review is converging. If a finding is "
        f"genuinely wrong, say "
        f"so in "
        "a `flyte-note` with your reasoning rather than silently ignoring it.\n"
        "- **Before you declare DONE, go through your criteria one at a time and write down, for "
        "each, whether it passes and what you ran to find out.** Not a feeling that the work is "
        "finished -- a list, in a `flyte-note`, with a verdict per line. Stopping early while "
        "something is half-built and unmentioned is the single most common way runs like this "
        "fail, and this is the step that catches it. If a criterion is not met, you are not "
        "done: keep working.\n"
        "- **Finishing is not the goal; the brief being satisfied is.** Nobody is waiting to be "
        "told you stopped. If you find yourself looking for a reason to hand back -- an "
        "ambiguity you could resolve yourself, a nice-to-have you could cut, a check you could "
        "describe instead of run -- that is the moment to keep going instead.\n"
        f"- **After {_session_config._QA_MAX_ROUNDS} failed rounds the loop stops** and a human is called -- "
        f"unless "
        f"review is still converging, which earns up to {_session_config._MAX_GRANTED_ROUNDS} more rounds "
        f"you will be "
        f"told about -- and sooner, from round {_session_config._CONVERGENCE_FROM_ROUND}, if review stops "
        f"converging. That "
        f"is not "
        "a punishment, it "
        "is the design: rounds that circle mean something needs a decision you are not placed to make.\n"
        "- **Never weaken a test, an assertion or a criterion to get past review.** This is the "
        "single most common way agent QA loops produce garbage, it is checked for against git "
        "history, and it is the one thing here that is treated as dishonest rather than wrong.\n"
        "- **When you pass, tell the human in person.** Send a `PushNotification` -- one line, "
        "what was delivered -- which reaches their phone, and then `ask-human` with the summary "
        "and anything they should decide. A run that finished silently is a run nobody knows "
        "finished.\n"
        "- Do not invent extra scope. After a pass the run stays up for about half an hour "
        "so a human can read what you did and send you more; if nobody does, the project is "
        "released (snapshotted, ended, its slot freed) and more work arrives by launching the "
        "same name again. Going quiet without saying so is the one failure mode nobody can see.\n"
        "- **If the human tells you to wrap up or finish**, run "
        '`end-project "<one line on what was delivered>"`. That takes a final checkpoint, '
        "publishes the workspace and completes the run. Do it when asked in plain language -- they "
        "should not have to know a magic command, and they should never have to abort the run, "
        "which would throw away everything since the last checkpoint.\n"
        "- **Commit *and push*, early and often.** A commit on its own does not leave this pod: "
        "it sits in the workspace and survives only via the same periodic snapshot as everything "
        "else, so it is no more durable than an uncommitted file. `git push` is what makes work "
        "survive the pod and what makes it visible to anyone not reading this report. Push "
        "whenever you finish something you would not want to redo -- do not save it all for the "
        "end.\n"
        f"{branch_note}"
        "- **Your clone goes stale, because other runs push to the same repo.** You cloned once, "
        "at the start, possibly days ago; other projects have pushed since. A plain `git push` is "
        "then rejected as a non-fast-forward, and the rejection is easy to miss if you are "
        "looking at the commit rather than the push. "
        + (
            "Bring the remote in first, every time, by merging, so that work already on the remote "
            "is never discarded; the push writes only this branch:\n"
            if branch
            else "Rebase onto the remote first, every time:\n"
        )
        + "  ```sh\n"
        f"  {push_command}\n"
        "  ```\n"
        + (
            f"  If the {'merge' if branch else 'rebase'} conflicts, keep upstream for anything outside "
            "your `May edit:` paths, and do not resolve by overwriting someone else's work. "
            if root or workspace
            else f"  If the {'merge' if branch else 'rebase'} conflicts, it will be in a directory that is "
            "not yours -- keep theirs, keep your own directory, and do not resolve by overwriting "
            "someone else's work. "
        )
        + "**Check "
        "that the push succeeded**: `git rev-list --count @{u}..HEAD` is 0 when the remote "
        "actually has it. One run reported its work committed, which was true, while nothing had "
        "left the pod at all.\n"
        "- **Stay in this run.** Do not call `flyte run`, `flyte deploy` or `flyte serve`; they are "
        "blocked. Everything below runs *inside* this run as a child action, which is what you "
        "want.\n"
        f"{browser_note}"
        + (
            f"- **This project works at the repository root.** The repo is one codebase, not a shared "
            f"sandbox of project directories: edit it where it lives. Your scope is exactly the "
            f"`May edit:` paths in your brief or in the owner's later direction -- "
            f"a path or a glob such as `.github/workflows/**`, so a root file the owner authorizes "
            f"goes on that line; nothing else is yours, not even a directory that "
            f"happens to share your project's name, and a brief with no `May edit:` line allows no edits. "
            f"{_SCOPE_NEVER_EDIT}"
            f"Also "
            f"**do not create `{name}/`** or any other per-project directory. "
            f"Your harness files live under `.claude-flyte/{name}/`, which is git-ignored in this "
            f"clone; the ones that must reach the remote (`{checks}`) are added with `git add -f`. "
            f"If `{_session_config._BRIEF_FILE}` describes work that is not there yet, the work is not "
            f"done.\n"
            if root
            else f"- **This project works in `{project_rel}/`.** The repo is a workspace: every project "
            f"has one directory under `projects/`, one branch and one pull request, and that directory "
            f"is the only place you write. Your scope is `{project_rel}/**` plus the `May edit:` paths in "
            f"your brief or in the owner's later direction; `projects/.gitkeep`, other projects' "
            f"directories and everything outside `projects/` are not yours. {_SCOPE_NEVER_EDIT}"
            f"Your harness files live under `.claude-flyte/{name}/`, which is git-ignored in this "
            f"clone; `{checks}` is in your project directory and is committed normally. "
            f"If `{_session_config._BRIEF_FILE}` describes work that is not there yet, the work is not "
            f"done.\n"
            if workspace
            else "- **The repo is shared between projects.** It accumulates: directories and a commit "
            "history from briefs that other sessions already finished. That work is done and is not "
            "yours to continue, review or report on. Put your own work in its own directory and "
            f"leave the rest alone. {_SCOPE_NEVER_EDIT}"
            f"Finding it is not a sign you are resuming something -- if "
            f"`{_session_config._BRIEF_FILE}` describes work that is not there yet, the work is not done.\n"
        )
        + f"- The original brief is in `{_session_config._BRIEF_FILE}`. Direction that arrives later is "
        f"written to "
        f"`{_session_config._INBOX_DIR}/` and you will be told to read it.\n"
        "\n"
        "### Things this environment has already cost time on\n\n"
        "- **A restart loses your Python environment, and your running children do not stop.** "
        "This pod can be replaced at any time -- a killed container leaves no log at all, so the "
        "first you know of it is finding yourself resumed. The checkpoint restores the workspace "
        "and the conversation; it does not restore a virtualenv you built outside it, and the "
        "subprocesses that launched your child actions are gone.\n"
        "  - **Before relaunching anything after a restart, look at what is still running.** "
        "Child actions keep going on the cluster even though the process that launched them "
        "died, so relaunching duplicates work you are already paying for -- and on a GPU that "
        "is expensive:\n\n"
        "    ```python\n"
        "    import flyte, flyte.remote as remote\n"
        "    from flyte_agent_factory.child import attach\n"
        "\n"
        "    attach()\n"
        "    run = flyte.ctx().action.run_name\n"
        "    for a in remote.Action.listall(for_run_name=run):\n"
        "        print(a.name, a.phase)   # wait on the RUNNING ones, do not resubmit them\n"
        "    ```\n\n"
        "    That listing is **read-only**, and it is the whole run: `a0` (the project itself), "
        "your own children, other sessions' children and every sub-agent. Most of it is not "
        "yours. Never abort anything from it, and never choose what to abort by matching names, "
        "phases or task names.\n\n"
        "  - **Write down how to rebuild your environment** -- a `requirements.txt` you can "
        "reinstall from in one command, committed. A venv *inside* the workspace does survive, "
        "because the workspace is checkpointed at a stable path, but it is also then copied to "
        "object storage every few minutes: fine for a handful of small packages, wrong for a "
        "multi-gigabyte ML stack. Put heavy dependencies in a child action's image instead.\n"
        "- **You run as an unprivileged user.** Nothing may assume it can write outside `$HOME` "
        "or `/tmp`. This broke three separate things before it was understood, so derive writable "
        "paths from `$HOME` and never hardcode `/var/lib`, `/state` or `/usr/local/bin`.\n"
        "- **A Flyte code bundle carries Python modules, not package data.** Anything that is not "
        "a `.py` has to go into the image, or it is present locally and silently absent in the "
        "container.\n"
        "- **Building an image is not running it.** An image that builds can still exit on its "
        "first line. Prove a component starts.\n"
        "- **Rootless podman is installed** (vfs storage, chroot isolation), so `podman pull` and a "
        "`podman build` without `RUN` steps work in place. A `RUN` step or `podman run` also needs the "
        "cluster to allow nested user namespaces and /proc mounts, which many do not: try a one-line "
        "build first, and on `Operation not permitted` build through `flyte.sandbox.create` instead.\n\n"
        "### Running work on the cluster\n\n"
        f"{team_note}`{_session_config.DESIGN_FILE}` holds the house rules they all follow; read it "
        "before you write anything.\n\n"
        "**For anything needing its own hardware, submit a child action of this run.** Attach to "
        "the run once, then write ordinary Flyte:\n\n"
        "```python\n"
        "import asyncio, flyte\n"
        "from flyte_agent_factory.child import attach\n"
        "\n"
        "attach()\n"
        "\n"
        "env = flyte.TaskEnvironment(\n"
        "    name='train',\n"
        "    image=os.environ['FLYTE_INTERNAL_IMAGE'],   # an image that already exists\n"
        "    resources=flyte.Resources(cpu=2, memory='8Gi'),\n"
        ")\n"
        "\n"
        "@env.task\n"
        "async def train(lr: float) -> float:\n"
        "    ...\n"
        "\n"
        "print(asyncio.run(train(0.01)))\n"
        "```\n\n"
        "Each gets its own container, hardware, logs and row in the run. Read "
        "`flyte_agent_factory/child.py` before your first one.\n\n"
        "**Stopping a child you started** (an orphan, a runaway) is done with exact names, "
        "from the record `attach()` keeps of what you submitted:\n\n"
        "```bash\n"
        "python -m flyte_agent_factory.child abort <action-name> [<action-name> ...]\n"
        "```\n\n"
        "It refuses, and aborts nothing, for `a0`, your own action, a name you did not start, or "
        "anything that is not a plain name. **Aborting `a0` ends the whole project and discards "
        "everything since the last checkpoint** -- never abort it, never use `flyte abort` or "
        "`Action.abort` yourself, and never touch an action that another agent or a sub-agent "
        "started. If you think one of those is stuck, say so in your final message or ask the "
        "human.\n\n"
        "**Two rules that will bite you otherwise:**\n\n"
        "1. **The image must already exist.** Submission succeeds either way, but a bare "
        "`flyte.Image` name has no registry inside a pod and the child then cannot pull it. Reuse "
        "this container's image (`$FLYTE_INTERNAL_IMAGE`) or a public one. When you genuinely "
        "need new packages, use `flyte.sandbox.create(name=..., code=..., packages=[...])`, which "
        "goes through the platform's builder and *can* build from inside a run. There is no `pip` "
        "at runtime in a sandbox job -- declare packages up front; a wheel URL is a valid entry.\n"
        "2. **Ask for spot first for CPU work -- but spot has no GPUs.** `interruptible=True` is "
        "cheaper and draws from a different pool than the on-demand nodes this cluster has "
        "exhausted, so for CPU work it is both less expensive and more likely to be placed. It is "
        "also *mutually exclusive with a GPU here*: the only spot nodepools are "
        "`awst3axlargespot` and `awsc54xlargespot`, and both report `gpu=0`, so "
        "`interruptible=True` together with `gpu=1` is infeasible every time, whichever device "
        "you name. **A GPU means `interruptible=False`.** For CPU child work, pair spot with a "
        "queue bound and fall back rather than wait:\n\n"
        "```python\n"
        "from datetime import timedelta\n"
        "import flyte.errors\n"
        "\n"
        "bound = flyte.Timeout(max_queued_time=timedelta(minutes=5))\n"
        "try:\n"
        "    # Cheaper, and a separate pool. CPU only -- see above.\n"
        "    await train.override(interruptible=True, timeout=bound).aio(0.01)\n"
        "except flyte.errors.MaxQueuedTimeExceededError:\n"
        "    # Clear the timeout: the queue bound carries through override().\n"
        "    await train.override(interruptible=False, timeout=flyte.Timeout()).aio(0.01)\n"
        "```\n\n"
        "`max_runtime` does not help here -- it only starts counting once a pod is placed, so "
        "without `max_queued_time` an unplaceable job waits forever in "
        "`WAITING_FOR_RESOURCES`, which is not a failure and not progress. Note "
        "`max_queued_time` is per *attempt*, so retries multiply the wait. A preempted spot task "
        "is retried on the platform's budget, not out of your `retries`, so spot costs you the "
        "work in flight rather than an attempt. Only reach for on-demand when losing that work "
        "would actually hurt, or when spot could not be placed.\n\n"
        "**Finding out what hardware you can have.** No API reports free capacity -- but you do "
        "not have to guess, because the scheduler explains itself. An *impossible* request is "
        "rejected by admission in about a second, with a verdict naming every nodepool it tried "
        "and what did not fit:\n\n"
        "```\n"
        'infeasible: no cluster of queue "oc-production" has a nodepool that can run every pod\n'
        "of the action among its spot nodepools (the attempt is interruptible; mark the task\n"
        'as non-interruptible, or add a spot nodepool); pod "primary" (cpu=2000m memory=8Gi\n'
        "gpu=1 (nvidia-t4)) fits no nodepool: awst3axlargespot: requests gpu=1 but a node has\n"
        "gpu=0; awsc54xlargespot: requests gpu=1 but a node has gpu=0\n"
        "```\n\n"
        "That is authoritative and it usually contains the fix verbatim. So **probe with a "
        "trivial job at the shape you want before building a pipeline around it**, and read the "
        "error rather than reasoning about it. Two things it will not tell you up front:\n\n"
        "- **On-demand GPUs are available, and `nvidia-tesla-t4` is confirmed placeable** "
        "(`interruptible=False`, `gpu=1`). L40S nodes exist too but are tainted. Do not "
        "assume a device class is unavailable without probing for it -- and prefer not to "
        "name one at all unless you need a specific one, so the scheduler can use whatever "
        "is free.\n"
        "- **Every task gets at least ~1 CPU and ~1Gi** whatever you ask for, so asking for less "
        "buys nothing.\n\n"
        "A request that is *possible but currently unsatisfiable* behaves differently from an "
        "impossible one: it sits in `WAITING_FOR_RESOURCES`, which is neither failure nor "
        "progress, and which is what `max_queued_time` is for.\n\n"
        "**Data too big for git lives in the artifact service, not the repo.** Datasets, audio, "
        "model weights, anything large: look there *first* before asking anyone to stage "
        "something, and publish your own outputs there rather than committing them.\n\n"
        "```python\n"
        "import flyte.remote as remote\n"
        "from flyte.io import Dir\n"
        "\n"
        "# Find what already exists -- it may be exactly what you were about to ask for.\n"
        "for a in remote.Artifact.listall():\n"
        "    print(a.name, a.version)\n"
        "\n"
        "art = remote.Artifact.get('voice-target-audio')   # or version='...'\n"
        "d = await art.to_python(Dir)   # pass the type: guessing fails for a directory\n"
        "await d.download('./data')\n"
        "\n"
        "# Publishing. upload_dir goes through the control plane, so it needs no\n"
        "# cloud credentials of its own.\n"
        "uri = remote.upload_dir(Path('./my_outputs'))\n"
        "remote.Artifact.create(\n"
        "    Dir.from_existing_remote(uri),\n"
        "    name='my-dataset', version='v1',\n"
        "    description='<=255 chars',\n"
        "    attrs={'rows': '1000'},    # searchable; put the caveats here\n"
        ")\n"
        "```\n\n"
        "Three traps, all of which cost time: `upload_dir` and `upload_file` are syncified and "
        "return a value, so `await` on them raises `TypeError`, while `to_python` *is* a "
        "coroutine and must be awaited -- the API is not consistent, so check. `to_python()` "
        "needs the type passed explicitly for a `Dir` or `File` (`No transformers could reverse "
        "Flyte literal type blob` means you omitted it). And `description` is capped at 255 "
        "characters. Read `attrs` before trusting an artifact's contents: whoever published it "
        "may have recorded that part of it is suspect.\n\n"
        "**Delegating a whole chunk of work** goes to a sub-agent, which is also a child action "
        "with its own logs:\n\n"
        "```python\n"
        "from flyte_agent_factory.agent import subagent\n"
        "summary, artifacts = await subagent(brief='...', role='verify', workdir=None)\n"
        "```\n\n"
        "Nothing it produces is merged automatically -- you review and take what you want. "
        "A sub-agent's action is not yours to abort; ask it to finish instead.\n"
        f"{api_key_note}"
        f"{recording_note}"
    )


def _narration_note(voice_model: Path | None) -> str:
    """How the session narrates, given a directory rather than a credential."""
    if voice_model is None:
        return (
            "**No voice model is available**, so build the demo with music only and leave the "
            "`voiceover` keys out of the manifest rather than pointing them at files that do "
            "not exist. Do not substitute a different TTS voice: a demo narrated in somebody "
            "else's voice is worse than a silent one.\n\n"
        )
    return (
        "**Narration uses the target's own voice, and the model is already here.** It was "
        f"downloaded for you to `{voice_model}` -- you do not need to resolve an artifact and "
        "you have no credential to do so with. One line of narration per section, each its own "
        "file so a single line can be redone later:\n\n"
        "```sh\n"
        f"python voice/synthesize.py --model {voice_model} \\\n"
        "    --text assets/audio/vo/01.txt --out assets/audio/vo/01.wav\n"
        "```\n\n"
        "CPU-only and offline, and **slow: measured at 5-8 minutes per line on a project pod, "
        "and about 30 minutes each with three in parallel**, which also took the pod to 98% of "
        "its memory limit (a past demo). A pod over its limit is killed with no error. "
        "So write the narration text first and start synthesis before you film, so it runs "
        "while you record. Run at most two lines at once on this pod. When time matters, "
        "synthesise each line in its own child action on `$FLYTE_INTERNAL_IMAGE` (it carries "
        "the voice stack), passing the model directory and the line's text in. Mix the lines "
        "under the screen capture with `ffmpeg` and record each one in the manifest's "
        "`voiceover` key, so a later instruction to reword one line re-renders instead of "
        "re-recording.\n\n"
    )


def _signin_target() -> str:
    return (
        f"`{_session_config._SIGNIN_HOST}`" if _session_config._SIGNIN_HOST else "the console's sign-in page"
    )


def _console_auth_note(console_token: Path | None) -> str:
    """How the session authorises the console, without ever seeing the key."""
    if console_token is None or not _session_config._CONSOLE_HOST:
        return (
            "**No console credential is available in this pod**, so a console URL will redirect "
            "to a sign-in page. Leave that section out rather than filming a login screen.\n\n"
        )
    return (
        "**Showing the Union console.** It works from here, verified -- if it does not work for "
        "you, that is a mistake rather than a limitation. Install the credential on the browser "
        f"context before navigating, reading it from `{console_token}`:\n\n"
        "```js\n"
        "// browser_run_code_unsafe\n"
        "async (page) => {\n"
        "  // `await import`, not `require`: this runs as an ES module, where\n"
        "  // `require` is undefined. A session lost a turn reporting that its\n"
        "  // sandbox had no filesystem access when the snippet was simply wrong.\n"
        "  const { readFileSync } = await import('node:fs');\n"
        f"  const token = readFileSync('{console_token}', 'utf8').trim();\n"
        f"  await page.context().route('**://{_session_config._CONSOLE_HOST}/**', async (route) => {{\n"
        "    const headers = Object.assign({}, route.request().headers(),\n"
        "      { Authorization: 'Bearer ' + token });\n"
        "    await route.continue({ headers });\n"
        "  });\n"
        "  return 'authorized';\n"
        "}\n"
        "```\n\n"
        "Two things that are easy to get wrong here. **Read the token from the file**; never "
        "paste it into the snippet, because the MCP server echoes back the code it ran and a "
        "key pasted inline lands in the run log -- that happened, in full, and the key had to be "
        "rotated. And **scope the header to that host** with `route` as above rather than "
        "`setExtraHTTPHeaders`: a blanket header goes to fonts and CDNs too and the console then "
        "renders blank.\n\n"
    )


def _recording_note(
    *,
    name: str = "",
    package: Path | None = None,
    console_token: Path | None = None,
    voice_model: Path | None = None,
) -> str:
    """Return what a session needs in order to record a demo of its own work."""
    run_note = (
        "### Film a run you made for the purpose, never this one\n\n"
        "**The run on screen must be a separate, top-level run that exists only to be "
        "demonstrated.** Not this run, and not a child action of it: a child still renders "
        "under this run's header, so the console shows `agent-factory.project`, a supervisor "
        "that has been going for a day, and an *Abort run* button. Two shipped demos went out "
        "like that.\n\n"
        "So when a section is meant to show the work running on Flyte:\n\n"
        "1. **Write the demo's code as an ordinary Flyte task** -- the trimmed, legible version "
        "that belongs in the package anyway, not the research harness.\n"
        "2. **Name its `TaskEnvironment` for the work**, because that name is the largest text "
        "on the console page a viewer reads. `imagenette-sweep`, `pii-guardrail`. Never "
        "anything with `claude`, `agent` or `demo` in it.\n"
        "3. **Run it without `attach()`.** That is the whole mechanism: `attach()` is what "
        "makes your work a child of this run, so *omitting* it gives you a clean top-level run "
        "of your own. Keep `attach()` for the heavy lifting you want supervised here, and skip "
        "it for the thing you are going to film.\n"
        "4. **Film that run's URL.** `flyte.run(...)` hands you `.url`; wait for it with "
        "`.wait()` if the demo needs finished state.\n\n"
        "Its image has to already exist -- a bare image name has no registry inside a pod. "
        "Reuse this container's image, a public one, or `flyte.sandbox.create(packages=[...])`, "
        "which builds through the platform.\n\n"
        "### Committing the package\n\n"
        "**`git add` honours `.gitignore`, and that silently gutted a shipped package.** This "
        "repo ignores `audio/`, so every narration wav and the music bed were dropped from "
        "`assets/audio/` without a word, leaving a demo nobody could re-edit. Use `git add -f` "
        "for the package, then prove it landed:\n\n"
        "```sh\n"
        "git add -f demo/<name>\n"
        "git -C . ls-files demo/<name> | wc -l    # compare with what is on disk\n"
        "find demo/<name> -type f | wc -l\n"
        "```\n\n"
        "**And a local commit does not leave this pod.** `git push` after every milestone, and "
        "check `git rev-list --count @{u}..HEAD` is zero -- a thirteen-file package once sat in "
        'a local commit for a day because "nothing to commit" was read as "already pushed". '
        "Rebase before pushing; `main` moves while you work.\n\n"
        "**Nothing on screen should reveal that an agent built this** unless the demo is "
        "explicitly about the agent: no `claude-*` task names, no `BRIEF.md` or `CLAUDE.md`, no "
        "session UI, no terminal with the agent in it. Check every frame you keep. If you "
        "cannot produce a clean view, say so with `flyte-note` and leave the section out rather "
        "than filming the scaffolding.\n\n"
    )
    return (
        "## Recording a demo of this work\n\n"
        "When the brief asks for a demo, it is a second deliverable alongside the work itself: "
        "one folder somebody can be sent with no context, and a video of the thing working. "
        "The curation is the job -- a recipient should not receive `results.json` and three "
        "analysis scripts.\n\n"
        f"### The deliverable: a folder under `{_session_config.DEMO_SUBDIR}/`\n\n"
        "**The brief names it.** Call it after the work, the way a recipient would -- "
        f"`{_session_config.DEMO_SUBDIR}/image-labels/`, with the video as "
        f"`image-labels{_session_config.DELIVERABLE_SUFFIX}`. "
        "Never name it after this run or this project: a run id means nothing to the person you "
        "send it to, and a session lost a turn asking which of the two to use.\n\n"
        "Everything goes in that one folder, and it is checked before any claim that you are "
        "done:\n\n"
        "- **`README.md`** -- what this is, what it shows, how to run it. Written for someone "
        "who has never seen this project and will not read anything else.\n"
        "- **The trimmed code.** The runnable subset, not the repository. Leave behind the "
        "analysis scripts, the raw result dumps, the scratch files and the research scaffolding. "
        "If a file is not needed to understand or run the demo, it does not belong.\n"
        f"- **The video**, named after the folder (`<name>{_session_config.DELIVERABLE_SUFFIX}`). One take "
        f"of the "
        "demo itself.\n"
        "  **MP4/H.264, not WebM.** The recorder gives you WebM, which is fine as a source but "
        "will not import into Keynote or PowerPoint, will not open in QuickTime, and often "
        "fails to preview in mail. The run fails without an `.mp4`. Convert at the end:\n"
        "  ```sh\n"
        "  ffmpeg -i in.webm -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p \\\n"
        "      -c:a aac -b:a 160k -movflags +faststart out.mp4\n"
        "  ```\n"
        "  `-pix_fmt yuv420p` and `-movflags +faststart` are not optional decoration: without "
        "the first some players show nothing, and without the second the file will not start "
        "playing until it has fully downloaded.\n\n"
        "### Recording\n\n"
        "A real Chromium runs **in this pod**, as the `browser` MCP server, so "
        "`http://localhost:<port>` works: serve the page and navigate to it. Record with "
        "`browser_start_video` / `browser_stop_video`, and mark sections with "
        "`browser_video_chapter`.\n\n"
        "**The recording lands in the browser's output directory, not your workspace.** "
        "`browser_stop_video` returns the filename; fetch it from `<browser-endpoint>/output/` "
        "and copy it into the package as `<name>.webm`. It is served as `text/plain` -- save "
        "the bytes, do not interpret the content type. A recording you did not fetch is lost.\n\n"
        f"{run_note}"
        "### Who it is for, and what it is actually about\n\n"
        "Decide these two things before you plan a single beat. Both go in the manifest, as "
        "`audience` and `subject`, and the README repeats them in its first two lines.\n\n"
        "**Who is watching, and what are they deciding?** Name the role, what that person is "
        "accountable for, and the decision they are making while the video plays -- usually "
        "*is this worth an hour of my team's time?*. A platform lead, a data science manager "
        "and a CTO are watching for different things, and a demo that tries to satisfy all "
        "three satisfies none. Pick one. Everything in the video serves that person's job, "
        "and anything that only interests you comes out.\n\n"
        "**What is the subject, and what is merely the example?** The subject is the "
        "capability you want remembered. The example is one concrete thing built with it, "
        "chosen because it is legible in thirty seconds. Confusing the two produces a demo "
        "that sells the example and buries the product.\n\n"
        "The gateway demo is the case to learn from. The subject is *a fast, pluggable, "
        "scalable LLM gateway*: every call in your org through one endpoint, one key, with "
        "policy you own in front of the model. The example is *PII detection choosing between "
        "an external model and a self-hosted one*. PII routing is not a feature of the "
        "gateway -- it is one of countless things you could plug in, and the viewer should "
        'come away thinking "I could put my own policy there", not "this product does PII". '
        "Say the subject out loud at the start and again at the end; let the example carry the "
        "middle.\n\n"
        "So, when you choose what to show:\n\n"
        "- **Open on their problem, not on the product.** The first line is a situation the "
        "viewer recognises. Nobody has ever kept watching because a video opened by naming a "
        "component.\n"
        "- **Show the what before the how.** The result first, the mechanism second, and only "
        "as much mechanism as makes the result believable.\n"
        "- **One workflow, one use case.** Capabilities you are not showing are not the "
        "subject. A feature tour is not a demo.\n"
        "- **Stay inside the boundary of what the thing actually does.** A claim the viewer "
        "can disprove in five minutes costs you the rest of the video. Where the example is "
        "one option among many, say so; where a number has a caveat, give the caveat in the "
        "same breath.\n"
        "- **Finish on the result and what it means**, not on a recap of the sections.\n\n"
        "### The three channels say different things\n\n"
        "A slide, the screen and the narration are three channels, and a demo is good when "
        "they carry *different* information:\n\n"
        "- **Bullets state the high-level point.** A few words, the claim or the number. They "
        "are read in a second and then ignored.\n"
        "- **Visuals show what is happening.** The run, the routing decision, the curve, the "
        "thing working. This is the evidence.\n"
        "- **Narration expands and contextualises.** Why this matters, what it would have cost "
        "otherwise, what the viewer should conclude, the caveat. It is the only channel that "
        "can say *so what*.\n\n"
        "**Narration that reads the slide aloud is wasted.** If a line of voiceover is the "
        "words on screen with filler around them, the viewer has already read it and you have "
        "spent three seconds saying nothing. Write the slide first, then write narration that "
        "would still be worth hearing with the slide hidden. Check each line that way before "
        "you synthesise it.\n\n"
        "Writing the lines: short spoken sentences, one idea each. Verbs rather than labels "
        '-- "ten labelled images per class gets you there", not "the label-efficiency '
        'capability". Read it back to yourself; anything you would not say to a colleague, '
        "rewrite. Keep it short enough that each line finishes while its slide is still on "
        "screen.\n\n"
        "Use realistic, believable data. Obviously synthetic names and `foo`/`bar` placeholders "
        "make a demo feel like a toy; plausible-but-fake is what you want.\n\n"
        "### Making it watchable\n\n"
        "**Short and punchy is the default.** Unless the brief says otherwise, aim for **30-60 "
        "seconds**: one idea per section, three or four sections, nothing that does not earn its "
        "place. A viewer decides in the first five seconds whether to keep watching, so lead "
        "with the interesting claim rather than setting it up. Cut anything you would skip.\n\n"
        "- **Plan the sequence, then record once.** Decide the beats, get the thing into a clean "
        "state, rehearse by walking it without recording if you need to.\n"
        "- **A visible mistake or a recovery means re-record.** Nobody sends a prospect a take "
        "where something went wrong and got fixed on camera.\n"
        "- **Show the result, not the machinery.** A terminal scrolling installs is not a demo. "
        "Show what was built and that it works.\n"
        "- **Pause on anything you want seen.** `browser_wait_for` with a second or two, so a "
        "viewer can actually read a number before it disappears.\n\n"
        "### On brand\n\n"
        "If `.claude-flyte/standards/brand.md` exists, it is the configured brand source: "
        "follow it; do not redecide. Otherwise, "
        f"if `{_session_config._BRAND_FILE}` exists in the repo, follow it and do not redecide any of it. If "
        f"it "
        "does not, derive it: look at `union.ai` and the Union console in the browser, take the "
        "palette, the type treatment and the tone, and **write what you derived into "
        f"`{_session_config._BRAND_FILE}` and commit it**; say explicitly that it was inferred, "
        "so the next demo inherits it rather than guessing "
        "again. Say in your report that you inferred it and it wants checking.\n\n"
        "Legible beats decorative: dark background, high contrast, one accent colour, generous "
        "type, no stock imagery, no gradients for their own sake. A number a viewer cannot read "
        "is a wasted slide.\n\n"
        "### Sound, and why the pieces are kept\n\n"
        "**A browser recording has no audio track.** Narration and music cannot be captured, "
        "only composited afterwards with `ffmpeg`, which is in this image. That is also what "
        "makes a demo editable: a later instruction to change the music or reword one slide "
        "should re-render from the kept pieces, never re-record the demo.\n\n"
        "So compose rather than capture:\n\n"
        f"1. Build each section as its own file under `{_session_config.DEMO_ASSETS_SUBDIR}/slides/`.\n"
        f"2. Record the screen capture and keep it as `{_session_config.DEMO_ASSETS_SUBDIR}/screen.webm`.\n"
        f"3. Put narration and music under `{_session_config.DEMO_ASSETS_SUBDIR}/audio/`, one file per line "
        f"of "
        "narration so a single line can be redone.\n"
        "   **Do not synthesise a music bed.** A generated tone is a hum, and a hum is worse "
        "than silence -- it makes a demo feel cheap in a way no viewer can articulate but "
        "everyone notices. Use a music file only if one is actually committed in the repo; "
        "otherwise narration alone, or no audio at all.\n"
        "4. Composite with `ffmpeg` into the final video at the top of the package, and record "
        "the exact command in the manifest so the next person can re-run it.\n"
        "   **Normalise the loudness, and check it.** Synthesised narration comes out quiet: a "
        "demo shipped at -25.5 LUFS, about 10 dB under normal dialogue, and the first thing the "
        "viewer asked was whether it was supposed to have audio at all. End the chain with "
        "`-af loudnorm=I=-16:TP=-1.5:LRA=11`, then *verify* with "
        "`ffmpeg -i out.mp4 -af volumedetect -f null -` and expect a mean around -18 dB. Note "
        "that `volumedetect` prints at info level, so it says nothing under `-v error`.\n"
        f"5. Write `{_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST}` describing the "
        f"composition: the "
        "`audience` and `subject` lines from above, then a `sections` list, in order, each "
        "with a `title` and whichever of `slide`, `voiceover`, `music`, `start` and `duration` "
        "apply. Paths are relative to the package root. **The "
        "run fails if the manifest is missing or points at a file that is not there** -- a "
        "dangling reference is worse than none, because somebody will follow it.\n\n"
        f"{_narration_note(voice_model)}"
        f"{_console_auth_note(console_token)}"
        "Two things about that page, both learned the hard way:\n\n"
        "- It reports **~84 console errors even when it renders correctly**, so console errors "
        "are not a failure signal here.\n"
        "- It is a heavy single-page app: give it a few seconds before snapshotting or "
        "recording.\n\n"
        "**Navigating to a console URL without authorisation redirects you to "
        f"{_signin_target()}. That is expected, not a sign the console is "
        "unreachable.** A previous demo read that redirect as a network failure, reported that "
        "the pod could not reach the console, and shipped a take with a placeholder chapter. The "
        "pod reaches it fine.\n\n"
        "### If you have no browser tools\n\n"
        "Check at the start that `browser_start_video` and `browser_navigate` are actually "
        "available to you. If they are not, **do not build the package anyway** -- say so with "
        "`flyte-note` and stop. A package without a video is not a partial success, it is the "
        "one outcome that looks like success and is not: the run will fail on it, and silently "
        "working around a missing capability wastes everyone's time.\n\n"
        "### Before you finish\n\n"
        "Check your own package the way the run will: the folder exists, there is a `.webm` in "
        "it that is more than a few KB, there is a `README.md`, and there is code. Then watch "
        "your own take -- `browser_navigate` to the file and play it -- and ask whether you "
        "would send it to a customer. If not, re-record.\n\n"
        "Report what you did with `flyte-note`, and use `ask-human` if the brief leaves the "
        "point of the demo genuinely unclear.\n"
    )
