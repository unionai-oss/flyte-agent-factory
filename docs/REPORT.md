# stopped-not-done: a stopped, unfinished project no longer shows as Done

Issue: unionai-oss/flyte-agent-factory#48. The copy is upstream `f83200e9dd2676b80cc9fee08e1486b06fc4fa41`
(main when this was delivered; `promote.toml`, from the operator's re-import, records it as the base) plus
this change.

## The bug

A session that exits cleanly after `/stop` ends with report phase `done`. `_board.run_passed()` counted
phase `done` as a pass, so `desired()` mapped a stopped run to Done, and the reconciler's five-minute pass
re-applied `desired()` over the operator's manual move back to Ready.

## What changed

- **`_board.run_passed`.** A run has passed only when its last verdict is PASS with no newer round, a legacy
  pass was restored (`review.passed` with no verdicts), it was released (a release only follows a pass), or
  its report was cut to an older truncated summary with no `review` whose `metrics.passed_round` records a
  pass with no newer round. That pass round goes through the same newer-round comparison as a verdict
  (`_review_state`), so a truncated round-1 pass does not count while round 2 is under review.
  Phase `done` alone is not a pass.
- **New Status `Stopped`** (between In review and Upstream). `desired()` maps a clean end without a pass
  (phase `done`, not passed) to Stopped. In `desired()`, failed or aborted runs still imply nothing, as before.
  Boards get the
  option from `bootstrap`, which already adds missing options in lifecycle order.
- **Supervisor.** At a clean end without a pass, the run moves its own card to Stopped
  (`_supervise_support._end_session_and_mark_stopped_card`). If that write fails, the reconciler computes the same value within five
  minutes. The end-of-run logic lives in `_supervise_support` because `agent.py` is held to its line budget
  (`test_the_modules_stay_inside_their_line_budgets`).
- **"Already ended" retry.** When an attempt finished and lost its pod before returning, the retry now
  restores the pass record into its timeline (`_supervise_support._restore_pass_and_end_retry`), so its report does not
  read as a stop.
- **Reconciler.** It does not overwrite a Status that a person set after the run's last event (the newest
  of the phase change, the review start and the last verdict). A run event newer than the move still
  applies, as before.
- **Drift (`_board_events.derive`).** Rule 9 (Done on an open issue whose newest run ended without a
  pass) now moves the card to Stopped instead of Ready when that run ended cleanly (Flyte phase `SUCCEEDED`,
  the clean stop this issue is about). A failed or aborted undelivered run still moves a Done card to Ready, as
  #175 made it, with one difference: if a person set Done after the run's last event, the move is held (the
  answer to question 3) rather than released after 24 hours. On a board whose PAT is a person's token, that
  also holds a Done the factory itself wrote after the run ended; since #48 the factory writes Done only for a
  pass, so this affects only cards written Done before this change. An existing draft PR or two quiet days do not
  displace Stopped, but newer PR events can move the card. A closed issue or a merged upstream PR still
  makes it Done, because those rules come first. A live run (a resume) moves a Stopped card to In
  flight (rule 6): Stopped ranks after In review, so without this rule 6 would treat the card as ahead of
  the run and leave it.

The merge path is unchanged: a merged PR or a closed issue still makes the card Done (drift rules 2 and 3,
the webhook's `_complete`, and `_write` keeping a closed issue's Done).

## Answers to the open questions

**1. Does the run snapshot record why a session exited? If not, what is the smallest signal that tells
them apart?**
Not as a structured field. The session-end text says "session ended by the human" or "the session
process exited", but only in the activity line and the timeline rows. The board does not need the reason,
though. What matters is whether review passed, and the snapshot already records that: the verdict list,
`review.passed`, `passed_round`, and phase `released`. The cases are told apart by:
- phase `done` with a pass: finished;
- phase `done` without a pass: a clean stop, whether it came from `/stop`, a session quitting on its own,
  or the review rounds running out;
- phase `failed`, or a Flyte run that failed or was aborted: a crash, which implies nothing, as before.

So the smallest signal is the pass state combined with the phase. No new field was added. A field
recording the exit reason would only help the dashboard, not the board.

**2. Should "stopped" be a board column of its own, or should the card go back to its status from before
the run?**
**Its own column, `Stopped`.** The status before the run is almost always Ready, because the planner
admits Ready cards. A card put back in Ready is relaunched by the planner as soon as a slot frees, which
defeats a `/stop` sent to free that slot. It also matters for benchmark cases, which always end with
`/stop`, and for the factory's own `/stop` to every live project on a secret rotation. Restoring a pre-run
status would also mean persisting it across pod restarts. Stopped says unfinished, the planner does not
admit it, and drift's stale rule does not apply to it. To resume, the owner moves the card to Ready (or
launches the same name). The new run then moves the card to In flight and In review like any other run,
because a new Flyte URL starts the card over (`_forward_only`, `_write`).

**3. Should the drift pass ever overwrite a status that a person set after the run's last event?**
**No, except for completion proven by GitHub** (a closed issue, a merged upstream PR), which is always
right. The reconciler now leaves Status alone when the card's Status was set after the run's newest event
by a setter that is not a `Bot`. A board PAT may be a person's own token, so the factory's own writes look
hand-set too. That is harmless here: a factory write always follows the run event it reflects, so it
already agrees with `desired()`, and only a real disagreement is held. A newer run event is new
information, so it still moves the card. The drift pass holds a hand-set Status the same way: a
correction is held until the evidence behind it (the run's newest event, a local pull request opened or
reopened, an upstream pull request opening or closing) is newer than the move, however old the move is.
That includes the two-day stale rule, a queued launch and a pull request card: elapsed time is not an event,
so a person who moved a card after the run's last event keeps that status through every later drift pass.
Upstream's 24-hour expiry (`HAND_SET`) is removed.

The cost of this choice: GitHub cannot tell the factory's writes through a person's PAT from that person's
own, so such a write is held the same way, until the run's next event (a resume, a review round). A card
whose last writer only looks like a person can then sit past two quiet days without going back to Ready.
That is preferred to undoing a real person's move, which is the failure the issue reports. When the
evidence or the identity is missing:
- no recorded run event (an undated report): nothing implies a newer status, so a hand-set Status is held,
  by the drift pass and the reconciler alike (`_board._hand_set_since_run`);
- no `status_at` or no setter on the card: the move cannot be dated or attributed, so it is not held;
- a setter GitHub reports as a `Bot`: never held, as in upstream, with one exception in the run projection:
  a Bot-set In review is not moved back to Stopped until the run has an event newer than that write,
  since it can reflect pull-request evidence that the stopped run does not know about.

A closed issue and a merged upstream pull request are never held: both are authoritative completion
evidence.

## Tests changed, and why

No upstream test was skipped or deleted, and no assertion was loosened. Upstream tests that were touched,
including the two whose expected value changes because of the issue's third question:

- `test_reconcile.py::test_legacy_pass_snapshot_is_done`: **the one assertion that encoded the bug.** It said
  `snapshot(phase="done")` with no verdicts maps to Done. That is exactly a clean exit after `/stop` before
  review, so its first assertion now expects `Stopped`. The rest of the test (a restored legacy pass, with
  `review.passed` set, is Done, or In review with a PR) is unchanged.
- `test_board_truth.py::test_done_after_undelivered_stop_moves_to_ready` (#175): unchanged apart from the
  `newest()` tuple gaining the Flyte phase (`FAILED`, `SUCCEEDED`). A failed run still moves Done to Ready.
- `test_board_truth.py::test_derive`: the rule-9 row `(Facts(stopped=True), "Done", "Ready")` is unchanged;
  a new row `(Facts(stopped=True, clean_stop=True), "Done", "Stopped")` covers the clean stop.
- `test_board_truth.py::test_hand_set_guard`: the case `(25, "User", True)` is now `(25, "User", False)`.
  The card is Done, set by a person 25 hours ago, and an open pull request that is no newer than that move
  would take it to In review. Upstream released the hold after 24 hours. Under the answer to question 3 a
  person's move is not overwritten without newer evidence, so the hold stays. This is the behaviour the issue
  asks for (the operator's Ready card was moved back 8 minutes later, and would have been moved back again
  after a day). The other three cases are unchanged.
- `test_board_truth.py::test_done_after_a_pass_whose_pr_merged_without_closing_the_issue_stays_done`: its
  "passed" fixture was `phase="done"` alone, which is no longer a pass. It now carries a PASS verdict, so it
  still tests what its name says.
- `test_board_truth.py::test_bootstrap_inserts_upstream_after_in_review_and_keeps_every_option` and
  `test_bootstrap_leaves_a_board_that_has_upstream_alone`, and
  `test_board_events.py::test_one_status_list_drives_ranks_and_order`: their expected Status lists gain
  `Stopped`, which is a new option. The ordering assertion is strengthened to In review < Stopped < Upstream
  < Done.
- `test_board_events.py`: the unmerged-PR parametrize gains a `Stopped` case.
- `test_supervision.py::test_clean_exit_still_has_a_final_row_after_pump_failure`: its setup moved into a
  `_run_project` helper so that the new end-move test can share it. Its assertions are unchanged.
- The fake `Board` in `test_reconcile.py` can now report when and by whom a Status was set (`updatedAt`,
  `creator`), as GitHub does. Writes are unstamped unless a test sets `write_at`, so existing tests see the
  same items as before.

New tests:

- `test_reconcile.py`:
  - `test_clean_exit_is_a_pass_only_after_review_passed`, which covers the issue's three acceptance cases:
    a clean exit after `/stop` before review and after a FAIL is Stopped, and after a PASS it is Done or In
    review. The table also covers released, failed and truncated summaries, live and not live.
  - `test_a_review_round_after_a_pass_then_a_stop_is_stopped`.
  - `test_legacy_pass_without_passed_round_passed_in_its_own_round`: a restored legacy pass from a report
    older than `passed_round` is still a pass, unless a newer round started after it.
  - `test_a_failed_run_still_implies_nothing`.
  - `test_truncated_report_keeps_its_pass_from_the_metrics`.
- `test_board.py`: `test_a_truncated_pass_does_not_count_once_a_newer_review_round_started` and
  `test_a_truncated_pass_with_no_newer_round_still_counts`; `test_dashboard.py`:
  `test_an_older_truncated_report_without_review_compares_its_pass_round_with_the_live_round`, through
  `from_report` on the persisted form. The newer-round cases fail on the previous implementation.
  - `test_run_event_at_is_the_newest_moment_the_run_reports` (the function is now `find_run_event_time`;
    `pull_request_evidence` is likewise `fetch_pull_request_evidence`).
  - `test_reconciler_keeps_a_hand_move_newer_than_the_run`, with cases for a person's move, an older move, a
    Bot setter and no `status_at`.
  - `test_reconciler_moves_a_stopped_run_to_stopped_and_then_leaves_a_hand_move`.
  - `test_reconciler_keeps_a_hand_move_when_the_report_has_no_dated_event`: an undated report holds a person's
    move, as the drift pass does; an unattributed one still moves.
  - `test_a_resume_moves_the_card_with_the_new_run`, from Stopped and from Done: In flight, then In review.
  - `test_supervisor_starts_a_resume_over_a_stopped_card`.
  - `test_supervisor_stopped_move_keeps_a_closed_issues_done`.
- `test_board_truth.py`:
  - `test_done_after_a_clean_stop_before_review_moves_to_stopped`.
  - `test_stopped_card_is_left_alone_with_its_own_pull_request_or_after_stale`.
  - `test_stopped_card_follows_a_resume_and_a_close`.
  - `test_derive_keeps_stopped_and_moves_a_live_resume`.
  - `test_stop_mid_build_never_reaches_done`: the acceptance scenario. It runs the supervisor's end move, a
    reconciler tick and a drift pass on fakes, and none of them leaves the card Done.
  - `test_done_snapshot_requires_a_successful_terminal_run`: through the real `RunIndex`, a report with
    phase `done` whose Flyte run failed or was aborted goes to Ready; one that succeeded goes to Stopped.
  - `test_failed_or_aborted_undelivered_run_leaves_done_for_ready_not_stopped`.
  - `test_a_later_hand_move_survives_repeated_stale_passes`: a person's In flight, set five days ago, after
    a failed run whose report is dated six days ago (or not dated), stays In flight through three drift
    passes.
  - `test_a_run_event_newer_than_the_hand_move_lets_a_stale_card_move`: with the run's event three days ago,
    newer than the move, the stale rule moves the card to Ready, for a `User`, a `Bot` and no setter alike.
  - `test_a_factory_write_through_a_persons_pat_holds_until_the_next_run_event`.

  These three replace `test_a_hand_set_card_still_goes_stale`, a test added earlier in this change (not an
  upstream test), which expected the stale rule to undo a five-day-old hand move by time alone. That is the
  behaviour review found contrary to the issue's third requirement; its coverage of stale hand-set cards is
  kept by the newer-event and PAT cases.
  - `test_old_hand_move_waits_for_a_new_run_event` and `test_hand_move_compared_with_upstream_pr_event`.
- `test_board.py::test_bootstrap_adds_stopped_between_in_review_and_upstream_keeping_ids`.
- `test_supervision.py::test_a_clean_end_without_a_pass_moves_the_card_to_stopped`: the real `agent.project`
  run on fakes. A clean end before review, or after a FAIL, moves the card to Stopped; a passed end does not.

The acceptance scenario runs against a fake board service; no live test-board run was performed because
no disposable board or authorization for temporary card moves was provided.

Test isolation fix, `tests/conftest.py::_reset_author_pin`: `agent.project` records its `engine` input in
`_pins.engine_input` and its `hard_pin` in `_usage._hard_pins`, and exports
`AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE`; nothing undid them. Upstream's
`test_codex_author.py::test_a_dead_login_on_the_codex_engine_input_ends_and_marks_codex_broken` runs it pinned
to codex, so `test_pin_and_credential.py::test_r2_the_author_is_claude_when_only_the_slot_login_is_limited`
failed (`'codex' == 'claude'`) whenever xdist placed it later on the same worker. The new `test_supervision.py`
cases changed the scheduling enough to expose it. The fixture resets all three before every test; no
assertion changed. The failure was seen in the supervisor's pre-review run of the whole suite; running
those two ids alone, in that order, does not reproduce it, so which earlier tests on the worker leave the state
behind is not pinned down. The fixture clears the shared state whatever ran before.

## Checks

`checks.sh` runs ruff format and lint, strict mypy and the whole pytest suite in both lanes, `not slow` and
`slow`, and exits 0 only when every step did. Given the factory's lane launcher (`AGENT_FACTORY_TEST_LANE`
with `AGENT_FACTORY_SLOW_LANE=child`, as `factory-improvement/checks.sh` uses it) the lanes are sharded across
child actions; otherwise they run in place. Pytest's "no tests collected" (exit 5) fails a lane, because each
lane is a whole marker of the suite.

## Which upstream this is based on

The copy is upstream `f83200e` with this change applied. Against a clean checkout of that commit, the only
differences are the files this report lists (`_board.py`, `_board_events.py`, `_dashboard.py`,
`_reconcile.py`, `_supervise_support.py`, `agent.py`, `docs/README.md`, `profiles/README.md` (the board
section: the Stopped status, rules 4a, 6 and 9, and the hand-move hold without a 24-hour expiry), six test
modules and
`tests/conftest.py`) plus the project's own `checks.sh` and this report.

An earlier copy was based on an older upstream commit, and rebasing it onto `f83200e` brought upstream #118's
renamed lock tests in `tests/test_deployer.py` and `tests/test_deployer_runs.py`, which the fail-to-pass gate
counted as tests this change removed. The operator resolved that by putting upstream `f83200e`'s blobs on
main and then re-importing the whole project at `f83200e`, so main's copy and this branch now share that base;
against main, this change removes no test and adds no skip.
