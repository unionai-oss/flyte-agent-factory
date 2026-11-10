# Smoke test: word statistics, checked three ways

Build `{name}/` (in its own directory, new for this run; leave everything else in the repo alone,
including any earlier `wordstats*` directories, which are other runs' work and prove nothing about this one).
This is deliberately small. It exists to prove the harness can start, use a browser,
run a child action, get reviewed, and finish without a person steering it.

## Deliverables

1. `{name}/wordstats.py` -- `top_words(text: str, n: int) -> list[tuple[str, int]]`:
   lower-cased, punctuation stripped, ties broken alphabetically. Plus a CLI:
   `python wordstats.py FILE [-n N]` printing `word<TAB>count` lines.
2. `{name}/test_wordstats.py` -- pytest tests including an empty string, ties,
   and punctuation.
3. **A child action.** Run the test suite as a Flyte *child action of your own run*
   (`from flyte_agent_factory.child import attach`, a `TaskEnvironment` using
   `$FLYTE_INTERNAL_IMAGE`, one `@env.task` that runs pytest and returns the pass
   count). Save the returned count and the child action's name in
   `{name}/CHILD.txt`. Prefer spot (`interruptible=True`) with a queue bound,
   per the memory.
4. **The browser.** Generate `{name}/report.html` showing the top 10 words of
   `BRIEF.md`, serve it on `http://localhost:<port>`, open it in the browser, and
   save a screenshot as `{name}/report.png`. The page must contain the word that
   ranks first.
5. `{name}/README.md`: how to run it, in under 15 lines.

## Done means

`{name}/ACCEPTANCE.md` (committed, written before you build; not the repo root, which other runs share) with a verdict per line, all
of it committed *and pushed*, then `DONE`. Nothing else is in scope.

## Notes

The output format of the CLI is as stated. Anything the brief does not say, decide it
yourself and record the decision in a `flyte-note` -- this is a small task and nobody
needs to be consulted about it.
