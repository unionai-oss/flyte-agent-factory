"""Triage of `ask-human` questions: the judge's prompt, answer format, waiver guard and replay CLI."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _judgement, _profile

ENGINEERING = (
    "CLASS: ENGINEERING\nDECISION: Put the new tools in sibling modules.\nREASONING: Leaves upstream alone."
)


def _prompt(question="Split _tools.py?", **overrides):
    args = {
        "kind": "text",
        "brief": "Build the thing.",
        "acceptance": "",
        "directions": [],
        "owner_reserved": _judgement.OWNER_RESERVED_FLOOR,
        "standing": _judgement.STANDING_RULES,
        "standards": "",
    }
    return _judgement.judge_prompt(question, **{**args, **overrides})


# --- the prompt ------------------------------------------------------------------------------


def _parsed(text: str) -> _judgement.Judgement:
    """Parse a call the test expects to be readable."""
    got = _judgement.parse(text)
    assert got is not None, text
    return got


def test_the_prompt_asks_for_the_right_thing_not_the_asked_options_or_the_stated_default():
    prompt = _prompt("Split it? (a) wait (b) split upstream (c) accept. Default: accept.")
    assert "what is the right thing to do" in prompt
    assert "not limited to the options the asker listed" in prompt
    assert "not swayed by the default" in prompt
    assert "Default: accept." in prompt  # the question itself is shown as asked


def test_the_waiver_declaration_tells_fixing_a_wrong_check_from_exempting_the_projects_code():
    """A live a0-004 judge declared WAIVER: yes for scoping module-size off a vendored checkout."""
    prompt = " ".join(_prompt().split())
    assert "exempting/excluding the project's own code from a rule" in prompt
    assert "no if it fixes a check that measures the wrong thing" in prompt
    assert "third-party or vendored code the project does not own" in prompt


def test_the_prompt_carries_the_standing_rules_and_the_reserved_floor():
    prompt = _prompt()
    assert "No waivers" in prompt and "never merges outside its sandbox" in prompt
    assert all(category in prompt for category in _judgement.OWNER_RESERVED_FLOOR)
    assert "Never choose a waiver" in prompt or "Never choose a waiver, a lowered threshold" in prompt


def test_the_prompt_gives_the_brief_as_authority_directions_standards_and_the_format():
    prompt = _prompt(
        brief="BRIEF-TEXT",
        acceptance="ACCEPT-TEXT",
        directions=["Union: use sibling modules", "Claude app: be quick"],
        standards="STANDARD-RULE",
    )
    assert "BRIEF-TEXT" in prompt and "ACCEPT-TEXT" in prompt and "STANDARD-RULE" in prompt
    assert "- Union: use sibling modules" in prompt and "- Claude app: be quick" in prompt
    assert "copy of the workspace" in prompt and "conventions" in prompt
    for label in ("CLASS:", "DECISION:", "APPROVE:", "REASONING:", "WHY OWNER:", "RECOMMENDATION:"):
        assert label in prompt


def test_a_profile_adds_to_the_floor_and_the_standing_rules_and_cannot_remove_them():
    prompt = _prompt(
        owner_reserved=(*_judgement.OWNER_RESERVED_FLOOR, "anything touching the billing tables"),
        standing=(*_judgement.STANDING_RULES, "prefer boring tools"),
    )
    assert "anything touching the billing tables" in prompt and "prefer boring tools" in prompt
    assert all(category in prompt for category in _judgement.OWNER_RESERVED_FLOOR)


def test_the_profile_section_parses_and_rejects_the_wrong_shape():
    base = _profile_text()
    parsed = _profile.parse(
        base + '\n[judgement]\nowner_reserved = ["billing"]\nstanding_direction = ["x"]\n', "example.toml"
    )
    assert parsed.judgement == _profile.JudgementPolicy(("billing",), ("x",))
    assert _profile.parse(base, "example.toml").judgement == _profile.JudgementPolicy()
    for bad in ('owner_reserved = "billing"', "owner_reserved = [1]", 'owner_reserved = [""]', "other = []"):
        with pytest.raises(_profile.ProfileError, match="judgement"):
            _profile.parse(base + f"\n[judgement]\n{bad}\n", "example.toml")


def _profile_text():
    return (Path(__file__).resolve().parent.parent / "profiles" / "example.toml").read_text()


# --- parse -----------------------------------------------------------------------------------


def test_parse_an_engineering_call_with_multiline_values():
    got = _parsed("CLASS: ENGINEERING\nDECISION: Do A.\n  Then B.\nAPPROVE: yes\nREASONING: because\nof C")
    assert got == _judgement.Judgement("engineering", "Do A.\n  Then B.", "because\nof C", True)


@pytest.mark.parametrize(
    ("approve", "expected"), [("yes", True), ("no", False), ("neither", None), ("", None), ("maybe", None)]
)
def test_parse_approve(approve, expected):
    text = f"CLASS: ENGINEERING\nDECISION: d\nAPPROVE: {approve}\nREASONING: r"
    assert _parsed(text).approve is expected


@pytest.mark.parametrize(
    ("waiver", "expected"), [("yes", True), ("no", False), ("No.", False), ("", None), ("maybe", None)]
)
def test_parse_waiver(waiver, expected):
    text = f"CLASS: ENGINEERING\nDECISION: d\nWAIVER: {waiver}\nREASONING: r"
    assert _parsed(text).waiver is expected


def test_parse_a_missing_waiver_field_is_not_declared():
    assert _parsed(ENGINEERING).waiver is None


def test_the_prompt_asks_the_judge_to_declare_whether_the_decision_is_a_waiver():
    prompt = _prompt()
    assert "WAIVER: yes | no" in prompt
    for phrase in (
        "waiving",
        "moving a threshold or limit",
        "exempting",
        "skipping, deleting or weakening a test",
    ):
        assert phrase in prompt


def test_parse_an_owner_call():
    got = _parsed("CLASS: OWNER\nWHY OWNER: spends money\nRECOMMENDATION: no, too dear")
    assert got.klass == "owner" and got.why_owner == "spends money" and got.recommendation == "no, too dear"


def test_parse_unsure_is_an_owner_call_marked_unsure():
    got = _parsed("CLASS: UNSURE\nWHY OWNER: maybe a credential\nRECOMMENDATION: ask")
    assert got.klass == "owner" and got.why_owner == "unsure: maybe a credential"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no fields at all",
        "DECISION: x\nREASONING: y",
        "CLASS: ENGINEERING\nREASONING: no decision",
        "CLASS: ENGINEERING\nDECISION: Split the module.",
        "CLASS: ENGINEERING\nDECISION: Split the module.\nREASONING:   ",
    ],
)
def test_parse_refuses_what_is_not_a_call(text):
    assert _judgement.parse(text) is None


async def test_decide_never_returns_an_engineering_decision_without_its_reasoning():
    """A decision is recorded for the owner to read and overrule; one with no reasoning cannot be judged."""
    got = await _judgement.decide(
        "q", "text", _caller("CLASS: ENGINEERING\nDECISION: Split it.\nREASONING:"), {}
    )
    assert got.klass == "owner" and got.why_owner.startswith("judge could not decide: ")


def test_parse_reads_labels_in_markdown_and_ignores_prose_before_them():
    got = _parsed("Here is my answer.\n\n**CLASS:** engineering\n**DECISION:** do it\nREASONING: ok")
    assert got.klass == "engineering" and got.decision == "do it"


# --- the waiver guard ------------------------------------------------------------------------


WAIVERS = [
    "Add a waiver for the failing check.",
    "Waive the module-size rule for _tools.py.",
    "Lower the threshold to 1600 lines.",
    "Raise the line limit to 2000.",
    "Remove the rule from the config.",
    "Disable the check in CI.",
    "Skip the flaky test.",
    "Mark the test xfail.",
    "Deselect the failing tests.",
    "Commit with --no-verify.",
    "Add # noqa to silence it.",
    "Add `# type: ignore` to the line.",
    "Fix the import. Then skip the test.",
    "Do not wait. Add a waiver.",
    # A negation that does not govern the match does not cancel it.
    "Rather than rewriting the module, add a waiver",
    "Not ideal, but add a waiver for ACME-012",
    "There is no good fix, so waive ACME-012",
    "Instead of splitting the file, raise the limit to 2100",
    "Without changing the code, skip the test",
    "Don't split the file; waive the rule",
    # Ways round a check that name no waiver.
    "mark the flaky test as skipped",
    "exclude the file from the module-size check",
    "Exempt _bridge.py from the size rule",
    "Add the file to the per-file-ignores list",
    "Raise max_lines to 2100",
    "Set the coverage threshold to 60%",
    "@pytest.mark.skipif",
    "Skip test_flaky in CI",
    "Comment out the failing assertion",
    "Add an entry to waivers.toml for ACME-012",
    "bump the limit to 2100",
    # Ways round a check the guard must catch now that it has the last word.
    "Add _tools.py to the allowlist",
    "Add the nested checkout to the exempt list",
    "Disable ACME-024 for this project",
    "Move the threshold to 2100",
    "Adjusting the module limit to 2100",
    "Grandfather _tools.py at its current size",
    "The gate has no fix so waive it",
    # Known false positives: the judge's WAIVER declaration resolves these.
    "Remove the duplicated check in _scan",
    "Remove the test from the xfail list now that it passes",
    "The waiver list is the owner's; leave it alone and fix the code",
]
NOT_WAIVERS = [
    "Do not add a waiver; fix the code.",
    "Fix the check rather than waiving it.",
    "No waiver: split the module.",
    "Fix the test instead of skipping the test.",
    "Split the module without lowering the threshold.",
    "Never remove the rule.",
    "Put the new tools in sibling modules and leave upstream's code alone.",
    "Fix the failing check so it passes on its merits.",
    "Fix the gate rather than waiving the rule",
    "No waiver: put the new tools in sibling modules",
    "Do not add a waiver; fix the check",
    "Increase the retry limit on the HTTP client from 3 to 5",
    "Ignore the test output directory in .gitignore",
    "Delete the stale test fixture file and regenerate it",
    "exclude the nested .flyte-sdk upstream checkout from the module-size walk",
    "Do not move the threshold to 2100; split the module",
    "Do not disable ACME-024; fix the code",
    "Never grandfather the file; split it",
]


@pytest.mark.parametrize("text", WAIVERS)
def test_proposes_waiver_catches_a_waiver(text):
    assert _judgement.proposes_waiver(text)


@pytest.mark.parametrize("text", NOT_WAIVERS)
def test_proposes_waiver_lets_a_refusal_through(text):
    assert not _judgement.proposes_waiver(text)


# --- decide ----------------------------------------------------------------------------------


def _caller(*replies):
    """A fake model: each reply is a string, or an exception to raise. Records the prompts."""
    prompts = []
    queue = list(replies)

    async def call(prompt):
        prompts.append(prompt)
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    call.prompts = prompts  # type: ignore[attr-defined]  # a test double
    return call


async def test_decide_returns_an_engineering_call():
    call = _caller(ENGINEERING)
    got = await _judgement.decide("q", "text", call, {"brief": "B"})
    assert got.klass == "engineering" and "sibling modules" in got.decision
    assert len(call.prompts) == 1 and "B" in call.prompts[0]


async def test_decide_passes_an_owner_call_through():
    got = await _judgement.decide(
        "q", "text", _caller("CLASS: OWNER\nWHY OWNER: money\nRECOMMENDATION: no"), {}
    )
    assert got.klass == "owner" and got.recommendation == "no"


@pytest.mark.parametrize("reply", ["garbage", RuntimeError("boom")], ids=["unparseable", "raises"])
async def test_decide_sends_what_the_judge_could_not_decide_to_the_owner(reply):
    got = await _judgement.decide("q", "text", _caller(reply), {})
    assert got.klass == "owner" and got.why_owner.startswith("judge could not decide: ")


async def test_decide_rejects_a_waiver_once_and_takes_the_second_answer():
    call = _caller("CLASS: ENGINEERING\nDECISION: Add a waiver.\nREASONING: r", ENGINEERING)
    got = await _judgement.decide("q", "text", call, {})
    assert "sibling modules" in got.decision
    assert "rejected" in call.prompts[1] and "Add a waiver." in call.prompts[1]


@pytest.mark.parametrize(
    "second",
    ["CLASS: ENGINEERING\nDECISION: Lower the threshold.\nREASONING: r", "garbage", RuntimeError("boom")],
    ids=["again", "unparseable", "raises"],
)
async def test_decide_falls_back_to_the_standing_rule_and_never_returns_a_waiver(second):
    call = _caller("CLASS: ENGINEERING\nDECISION: Skip the test.\nREASONING: r", second)
    got = await _judgement.decide("q", "text", call, {})
    assert got.klass == "engineering" and "no waiver, lowered threshold" in got.decision
    assert "rejected" in got.reasoning and not _judgement.proposes_waiver(got.decision)


def _reply(decision, waiver=None):
    return (
        f"CLASS: ENGINEERING\nDECISION: {decision}\n"
        + (f"WAIVER: {waiver}\n" if waiver else "")
        + "REASONING: r"
    )


async def test_decide_re_judges_a_declared_waiver_the_guard_cannot_see():
    call = _caller(_reply("Park the failing case.", "yes"), ENGINEERING)
    got = await _judgement.decide("q", "text", call, {})
    assert "sibling modules" in got.decision
    assert "WAIVER:" in call.prompts[1] and "Park the failing case." in call.prompts[1]


async def test_decide_names_the_phrase_the_guard_matched_when_it_asks_again():
    call = _caller(_reply("Add a waiver for ACME-012.", "no"), ENGINEERING)
    await _judgement.decide("q", "text", call, {})
    assert "Add a waiver" in call.prompts[1] and "honestly" in call.prompts[1]


@pytest.mark.parametrize(
    "second",
    [_reply("Park the case.", "yes"), _reply("Skip the test.", "yes"), _reply("Skip the test."), "garbage"],
    ids=["declared", "declared-and-matched", "matched-undeclared", "unparseable"],
)
async def test_decide_replaces_a_waiver_the_judge_still_declares_with_the_standing_rule(second):
    call = _caller(_reply("Park the case.", "yes"), second)
    got = await _judgement.decide("q", "text", call, {})
    assert "no waiver, lowered threshold" in got.decision and "rejected" in got.reasoning


@pytest.mark.parametrize("waiver", ["no", None, "yes"], ids=["declared-no", "missing", "declared-yes"])
async def test_decide_never_records_a_decision_the_guard_still_matches_whatever_the_judge_declares(waiver):
    """The phrase guard is final (this replaces two tests that asserted the opposite).

    They asserted that a guard match the judge disputed with `WAIVER: no` was accepted and recorded for the
    owner. That is the escape hatch the no-waiver rule forbids: the guard is the backstop because the
    declaration can be wrong.
    """
    call = _caller(
        _reply("Add a waiver for ACME-012.", "yes"), _reply("Exempt _bridge.py from the size rule.", waiver)
    )
    got = await _judgement.decide("q", "text", call, {})
    assert got.decision == _judgement._NO_WAIVER_DECISION
    assert "Exempt _bridge.py from the size" in got.reasoning


async def test_decide_replaces_the_reviewers_size_limit_reproduction_with_the_standing_rule():
    answer = _reply("Raise the module size limit to 2100 for _tools.py.", "no")
    got = await _judgement.decide("q", "text", _caller(answer, answer), {})
    assert got.decision == _judgement._NO_WAIVER_DECISION
    assert "Raise the module size limit" in got.reasoning


async def test_the_re_judge_prompt_does_not_promise_a_way_round_the_guard():
    call = _caller(_reply("Add a waiver.", "no"), ENGINEERING)
    await _judgement.decide("q", "text", call, {})
    assert "replaced by the standing rule" in call.prompts[1]
    assert "accepted" not in call.prompts[1]


async def test_decide_accepts_a_first_answer_the_judge_declares_clean_and_the_guard_passes():
    call = _caller(_reply("Split the module.", "no"))
    got = await _judgement.decide("q", "text", call, {})
    assert got.decision == "Split the module." and len(call.prompts) == 1


async def test_decide_accepts_an_owner_call_on_the_second_try():
    call = _caller(
        "CLASS: ENGINEERING\nDECISION: Waive it.\nREASONING: r",
        "CLASS: OWNER\nWHY OWNER: x\nRECOMMENDATION: y",
    )
    assert (await _judgement.decide("q", "text", call, {})).klass == "owner"


# --- the replay CLI --------------------------------------------------------------------------


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH that logs its argv and stdin and answers with $FAKE_JUDGE_REPLY."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "call = {'argv': sys.argv[1:], 'stdin': sys.stdin.read()}\n"
        "open(os.environ['FAKE_JUDGE_LOG'], 'a').write(json.dumps(call) + '\\n')\n"
        "print(os.environ['FAKE_JUDGE_REPLY'])\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_JUDGE_LOG", str(tmp_path / "log.jsonl"))
    return tmp_path / "log.jsonl"


def _fixture(tmp_path, **overrides):
    fixture = {
        "name": "split-upstream",
        "question": "Split _tools.py? (a) wait (b) split (c) accept. Default: accept.",
        "kind": "text",
        "brief": "Add tools.",
        "directions": ["Union: no waivers"],
        "context": "_tools.py is 1600 lines; the limit is 1500.",
        "expect": {"class": "engineering"},
        **overrides,
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    return path


def test_replay_runs_the_production_prompt_through_claude_and_passes(
    tmp_path, fake_claude, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_JUDGE_REPLY", ENGINEERING)
    assert _judgement.main(["replay", str(_fixture(tmp_path)), "--model", "opus"]) == 0
    out = capsys.readouterr().out
    assert "split-upstream" in out and "engineering" in out and "sibling modules" in out
    assert "waiver guard: clear" in out and "PASS" in out
    call = json.loads(fake_claude.read_text().splitlines()[0])
    assert call["argv"] == ["-p", "--output-format", "text", "--model", "opus"]
    assert "what is the right thing to do" in call["stdin"]
    assert "Union: no waivers" in call["stdin"]
    assert "What the workspace shows" in call["stdin"] and "1600 lines" in call["stdin"]


def test_replay_exits_nonzero_on_a_fail_and_reports_the_waiver_guard(
    tmp_path, fake_claude, monkeypatch, capsys
):
    monkeypatch.setenv("FAKE_JUDGE_REPLY", "CLASS: ENGINEERING\nDECISION: Add a waiver.\nREASONING: r")
    fixture = _fixture(tmp_path, expect={"class": "owner"})
    assert _judgement.main(["replay", str(fixture)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "waiver guard: rejected" in out and "no waiver, lowered threshold" in out


def test_replay_fails_a_guard_match_the_judge_declares_clean(tmp_path, fake_claude, monkeypatch, capsys):
    reply = "CLASS: ENGINEERING\nDECISION: Add a waiver for ACME-024 on _tools.py.\nWAIVER: no\nREASONING: r"
    monkeypatch.setenv("FAKE_JUDGE_REPLY", reply)
    fixture = _fixture(tmp_path, name="a0-004", expect={"class": "engineering", "no_waiver": True})
    assert _judgement.main(["replay", str(fixture)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "no waiver, lowered threshold" in out


def test_replay_of_an_owner_question_prints_why_owner(tmp_path, fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_JUDGE_REPLY", "CLASS: OWNER\nWHY OWNER: spends money\nRECOMMENDATION: no")
    assert _judgement.main(["replay", str(_fixture(tmp_path, expect={"class": "owner"}))]) == 0
    assert "why owner: spends money" in capsys.readouterr().out


def test_importing_the_module_runs_nothing_and_the_entry_point_is_guarded(tmp_path, fake_claude):
    env = {**os.environ, "PYTHONPATH": "src", "FAKE_JUDGE_REPLY": ENGINEERING}
    run = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._judgement", "replay", str(_fixture(tmp_path))],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0 and "PASS" in run.stdout
    assert 'if __name__ == "__main__":' in open("src/flyte_agent_factory/_judgement.py").read()
