# Context guard

The coordinator is one long Claude Code session. Every turn re-sends its whole context (cache reads are about 97% of
its tokens), so a 40 KB file read once with `cat` is paid for again on every later turn, and a session that never
compacts grows from 190K to 690K tokens per call. The context guard is a switch that trims what the session starts
with and stops the most expensive reads. It is **on by default**; switched off, CLAUDE.md and `settings.json` are
byte-for-byte what they were before the guard existed.

## Turning it off

Set `AGENT_FACTORY_CONTEXT_GUARD=0` in the run's environment (`false`, `no` and `off` also work, in any case). Unset,
empty or any other value leaves the guard on. In a bench experiment the policy arm works both ways:
`{"CONTEXT_GUARD": "0"}` is guard off and `{}` or `{"CONTEXT_GUARD": "1"}` is guard on; the arm becomes that
environment variable in the session's pod.

## Why it is on by default

The owner turned it on (2026-11-08) although the bench below could not decide it, because outside evidence points
the same way and the downside is small:

- Anthropic's [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  advises the smallest set of high-signal tokens in context, and compaction as a session nears the context limit.
- Claude Code's [cost documentation](https://docs.claude.com/en/docs/claude-code/costs) notes that token cost scales
  with context size and advises keeping CLAUDE.md lean and compacting long sessions.
- Chroma's [Context Rot](https://research.trychroma.com/context-rot) study found that model quality degrades as
  input context grows, even on simple tasks.

The guard never changes what a read returns, only refuses a whole-file read of a large file once and says how to
read a range, so the worst case is one extra turn.

## Production check

Because the bench was confounded, the default is checked in production and switched off if it does harm:

- **Coordinator tokens.** Compare per-run `tokens.coordinator` (the main conversation, cache-read included) in the
  run reports before and after this lands. It should fall; if it rises, switch the guard off.
- **Review passes that fail hidden checks.** In the A/B one guarded case (feature-expiry, repeat 1) passed review
  but failed the bench's hidden check. The unguarded A/A baseline also had one such case (feature-expiry,
  repeat 2). Use both as context when watching that pattern in bench runs and post-merge fixes to work that
  passed review; if it becomes more frequent than before, switch the guard off.

## What it does

- **Trims CLAUDE.md.** The demo-recording, narration and console-for-demo sections (about 14 KB in a browser
  project) are left out unless the launch is a demo (the factory knows its demo kind) or the brief asks for one: the words demo, video, recording, walkthrough or
  screencast, as whole words in any case. When a demo is asked for, the console-auth block appears once, not twice.
  A short "Context hygiene" paragraph is added.
- **Denies unbounded reads of large files.** A PreToolUse hook (`python -m flyte_agent_factory._context_guard`,
  matcher `Read|Bash`) refuses `Read` with no `offset` and no `limit`, and a plain `cat F`, `less F` or `more F`, on a
  text file over 24 KB. The reply gives the size, says the context is re-sent every turn, and shows how to read
  a range: `sed -n '1,200p' F`, `grep -n PATTERN F`, or `Read` with `offset` and `limit`. Ranged reads, anything
  piped, redirected or chained, small or missing files, and images, PDFs and notebooks are allowed. On any parse
  doubt or error the hook allows the call and prints nothing.
- **Compacts earlier.** The coordinator's environment gets `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` (Claude Code
  2.1.287 honours it; the minimum is 100000), so compaction fires at roughly 200K tokens of context. That is
  high enough that a normal working set survives the summary, and a third of what an uncompacted session reached.

Only the coordinator's pod gets the hook and the compaction window: its in-pod helpers (Agent-tool sub-agents) share
its settings and so get the hook too; review sub-agents and `delegate` workers, which run in their own child actions,
do not.

## Knobs

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_FACTORY_CONTEXT_GUARD` | unset (on) | `0`, `false`, `no` or `off` turns the guard off |
| `AGENT_FACTORY_CONTEXT_GUARD_BYTES` | `24576` | File size above which an unbounded read is denied |

## Why deny and redirect, not summarise

The hook never rewrites or summarises what a read returns. Edits, debugging and review need the exact source, and a
summary that is wrong in one line is worse than no summary. Denying costs the agent one turn and leaves the choice of
range with the one who knows what it is looking for.

## Measured effect (factory-bench)

These runs were made while the guard was off by default. All runs use the same engine SHA in both arms, with arm A
the policy `{}` (then: guard off) and arm B `{"CONTEXT_GUARD": "1"}`, so the
guard is the only difference. "Coordinator" is the main conversation only (`tokens.coordinator`, without in-pod
helpers).

### Pilot: 2 tasks x 1 repeat x 2 arms, about $49

| task | arm | accepted | review rounds | outcome | coordinator cache-read | usd |
|---|---|---|---|---|---|---|
| docs-standards | guard on | yes | 3 | accepted | 6.19M | 15.00 |
| bugfix-shortlinks | guard on | yes | 3 | accepted | 6.41M | 16.60 |
| docs-standards | guard off | no | 2 | capped_cost | 6.50M | 13.60 |
| bugfix-shortlinks | guard off | no | 1 | capped_time | 3.21M | 4.05 |

The pilot showed that each case records the coordinator split. With one repeat and no A/A history it decides
nothing (verdict "inconclusive", band assumed). The powered runs follow.

### Powered runs: 6 tasks x 2 repeats x 2 arms, A/B and A/A

One A/B run (arm A `{}`, arm B `{"CONTEXT_GUARD": "1"}`) and one A/A run (both arms `{}`), engine SHA `01fa4db`, 24
and 24 cases, about $217 and $245. The case records are committed in
`factory-improvement/bench/context-guard/{ab,aa}.jsonl`; `python factory-improvement/context_guard_bench.py report`
regenerates the tables below from them. The guard table has no `coord_cache_read` row because `verdict()` stops
at the visible pass shortfall before it gets to that guard.

**Result: no measured reduction.** These runs do not show that the guard lowers coordinator cache-read per accepted
case, and they do not show that it leaves the accepted rate alone. The issue's acceptance test (lower, outside the
A/A band, no drop in accepted rate) is **not met**. On these runs alone the guard stayed off; the owner later turned
it on by default for the reasons above.

**Why the runs cannot decide it: a shared-login confound.** Both runs ran at the same time on one subscription
login, and the work login hit its 5-hour usage limit more than once while they ran. Of the 48 cases, 29 waited
between 20 minutes and 2.8 hours for the limit to reset, and most of those ended `capped_time` at the 3-hour cap.
Whether a case was accepted then depends mostly on when its waits fell, not on its arm: the A/A, where nothing
differs between the arms, swings as much as the A/B. Pooled numbers per accepted case divide by that noisy count.
The bench's own verdict on the A/B is "worse", from the visible pass rate (6 of 12 accepted with the guard, 8 of 12
without); in the A/A the two identical arms gave 7 of 12 and 5 of 12.

**The matched comparison that avoids the waits is within noise.** Taking only verified-accepted cases that never
waited, per task, the guard's cases read 0.73 of the baseline's coordinator cache-read (geometric mean over the 2
tasks where both arms had such a case). The same comparison on the A/A, where the arms are identical, gives 0.82
(over 2 tasks). The guard's apparent saving is the size of the noise, and two or three tasks are too few to tell
them apart. One guarded case (feature-expiry, repeat 1) passed review but failed the hidden check, so it is not
counted as accepted. The unguarded A/A baseline also had one such case (feature-expiry, repeat 2), so this
observation does not by itself show that the guard costs quality.

**What was not verified.** The hook was run as Claude Code runs it, a subprocess given a PreToolUse payload on
stdin, and denied a whole read of a 55 KB file while allowing a ranged one. Whether it fired inside the guarded
bench sessions was not checked: the case records keep token counts, not transcripts.

**What it would take.** Run the A/B again alone on a login with headroom, with nothing else on it, so cases do not
wait; the committed A/A then serves as the band only if it is itself free of waits, otherwise run a fresh A/A the
same way. That re-run is left to the operator, who will launch it when the login allows.

#### A/B: guard off (baseline) vs on (candidate); band assumed, A/A too small to calibrate (4 matched tasks, 6 needed)

| arm | cases | accepted | mean review rounds (accepted) | coordinator cache-read per accepted case | usd |
|---|---|---|---|---|---|
| baseline | 12 | 8 | 1.75 | 7.15M | 115.48 |
| candidate | 12 | 6 | 1.50 | 7.99M | 101.08 |

Bench verdict (min sample relaxed to what ran): **worse** (visible pass shortfall), m=4 matched tasks, cost band b=0.246 (assumed).

| guard | S (mean log ratio B/A) | 90% interval | band b | verdict |
|---|---|---|---|---|
| cost | n/a | n/a .. n/a | 0.246 | - |

#### A/A: both arms guard off (the noise it measures)

| arm | cases | accepted | mean review rounds (accepted) | coordinator cache-read per accepted case | usd |
|---|---|---|---|---|---|
| baseline | 12 | 5 | 1.60 | 12.79M | 132.70 |
| candidate | 12 | 7 | 1.86 | 7.98M | 112.09 |

Bench verdict (min sample relaxed to what ran): **inconclusive** (no A/A band), m=4 matched tasks, cost band b=0.246 (assumed).

| guard | S (mean log ratio B/A) | 90% interval | band b | verdict |
|---|---|---|---|---|
| cost | +0.098 | -0.679 .. +0.773 | 0.246 | - |
| time | +0.615 | -0.023 .. +1.236 | 0.246 | inconclusive |
| first_pass | +0.125 | -0.500 .. +0.750 | 0.246 | inconclusive |
| coord_cache_read | +0.076 | -0.793 .. +0.898 | 0.246 | inconclusive |

#### Sensitivity: cases that waited on the usage limit left out

| arm | cases | accepted | mean review rounds (accepted) | coordinator cache-read per accepted case | usd |
|---|---|---|---|---|---|
| baseline | 6 | 6 | 1.83 | 4.99M | 70.68 |
| candidate | 4 | 3 | 1.00 | 4.92M | 30.24 |

Bench verdict (min sample relaxed to what ran): **inconclusive** (underpowered), m=2 matched tasks, cost band b=0.348 (assumed).

| guard | S (mean log ratio B/A) | 90% interval | band b | verdict |
|---|---|---|---|---|
| cost | n/a | n/a .. n/a | 0.348 | - |

#### Matched no-wait accepted cases, A/B

| task | baseline cases | baseline mean | candidate cases | candidate mean | ratio B/A |
|---|---|---|---|---|---|
| bugfix-shortlinks | 2 | 4.63M | 1 | 2.17M | 0.47 |
| docs-standards | 1 | 3.41M | 1 | 3.91M | 1.15 |
| feature-expiry | 2 | 6.00M | 0 | n/a | n/a |
| ml-pipeline | 0 | n/a | 1 | 5.05M | n/a |
| refactor-logstats | 0 | n/a | 0 | n/a | n/a |
| tf-bucket | 1 | 5.25M | 0 | n/a | n/a |

2 tasks with a no-wait accepted case in both arms; geometric mean ratio 0.73 (mean log ratio -0.309).

#### Matched no-wait accepted cases, A/A (same comparison where nothing differs)

| task | baseline cases | baseline mean | candidate cases | candidate mean | ratio B/A |
|---|---|---|---|---|---|
| bugfix-shortlinks | 1 | 4.58M | 1 | 4.10M | 0.90 |
| docs-standards | 1 | 3.37M | 0 | n/a | n/a |
| feature-expiry | 0 | n/a | 1 | 5.69M | n/a |
| ml-pipeline | 1 | 13.74M | 1 | 10.32M | 0.75 |
| refactor-logstats | 2 | 5.88M | 0 | n/a | n/a |
| tf-bucket | 0 | n/a | 0 | n/a | n/a |

2 tasks with a no-wait accepted case in both arms; geometric mean ratio 0.82 (mean log ratio -0.198).

#### Cases, A/B

| task | arm | repeat | outcome | review rounds | waited on limit (min) | coordinator cache-read | usd |
|---|---|---|---|---|---|---|---|
| bugfix-shortlinks | baseline | 1 | accepted | 2 | 0 | 4.52M | 11.21 |
| bugfix-shortlinks | baseline | 2 | accepted | 2 | 0 | 4.75M | 11.91 |
| bugfix-shortlinks | candidate | 1 | accepted | 1 | 0 | 2.17M | 5.41 |
| bugfix-shortlinks | candidate | 2 | capped_time | 2 | 142 | 4.07M | 11.34 |
| docs-standards | baseline | 1 | accepted | 1 | 0 | 3.41M | 6.25 |
| docs-standards | baseline | 2 | accepted | 1 | 21 | 3.16M | 6.57 |
| docs-standards | candidate | 1 | accepted | 1 | 0 | 3.91M | 7.45 |
| docs-standards | candidate | 2 | capped_time | 0 | 116 | 0.00M | 0.00 |
| feature-expiry | baseline | 1 | accepted | 2 | 0 | 5.56M | 12.95 |
| feature-expiry | baseline | 2 | accepted | 2 | 0 | 6.45M | 14.10 |
| feature-expiry | candidate | 1 | accepted | 1 | 0 | 3.63M | 8.03 |
| feature-expiry | candidate | 2 | accepted | 2 | 108 | 8.66M | 16.81 |
| ml-pipeline | baseline | 1 | capped_time | 1 | 124 | 7.33M | 11.05 |
| ml-pipeline | baseline | 2 | capped_time | 0 | 143 | 3.29M | 4.58 |
| ml-pipeline | candidate | 1 | capped_time | 0 | 141 | 1.35M | 1.53 |
| ml-pipeline | candidate | 2 | accepted | 1 | 0 | 5.05M | 9.35 |
| refactor-logstats | baseline | 1 | capped_time | 0 | 166 | 2.48M | 3.14 |
| refactor-logstats | baseline | 2 | capped_time | 1 | 150 | 5.09M | 5.80 |
| refactor-logstats | candidate | 1 | capped_time | 1 | 160 | 4.12M | 4.63 |
| refactor-logstats | candidate | 2 | capped_time | 2 | 142 | 6.56M | 11.67 |
| tf-bucket | baseline | 1 | accepted | 2 | 0 | 5.25M | 14.27 |
| tf-bucket | baseline | 2 | accepted | 2 | 86 | 5.90M | 13.67 |
| tf-bucket | candidate | 1 | accepted | 2 | 62 | 4.38M | 12.75 |
| tf-bucket | candidate | 2 | accepted | 2 | 60 | 4.03M | 12.11 |

#### Cases, A/A

| task | arm | repeat | outcome | review rounds | waited on limit (min) | coordinator cache-read | usd |
|---|---|---|---|---|---|---|---|
| bugfix-shortlinks | baseline | 1 | accepted | 2 | 0 | 4.58M | 12.99 |
| bugfix-shortlinks | baseline | 2 | capped_time | 1 | 166 | 3.01M | 2.96 |
| bugfix-shortlinks | candidate | 1 | accepted | 2 | 0 | 4.10M | 11.05 |
| bugfix-shortlinks | candidate | 2 | accepted | 1 | 36 | 2.50M | 5.14 |
| docs-standards | baseline | 1 | accepted | 1 | 0 | 3.37M | 6.79 |
| docs-standards | baseline | 2 | capped_time | 0 | 116 | 0.00M | 0.00 |
| docs-standards | candidate | 1 | capped_time | 1 | 73 | 3.21M | 2.81 |
| docs-standards | candidate | 2 | accepted | 2 | 46 | 5.29M | 11.89 |
| feature-expiry | baseline | 1 | escalated | 3 | 0 | 7.62M | 25.49 |
| feature-expiry | baseline | 2 | accepted | 1 | 69 | 4.38M | 9.30 |
| feature-expiry | candidate | 1 | capped_time | 1 | 157 | 3.25M | 5.55 |
| feature-expiry | candidate | 2 | accepted | 2 | 0 | 5.69M | 12.41 |
| ml-pipeline | baseline | 1 | accepted | 2 | 0 | 13.74M | 21.57 |
| ml-pipeline | baseline | 2 | capped_time | 1 | 81 | 8.19M | 11.81 |
| ml-pipeline | candidate | 1 | accepted | 2 | 0 | 10.32M | 19.53 |
| ml-pipeline | candidate | 2 | capped_time | 1 | 137 | 5.04M | 7.36 |
| refactor-logstats | baseline | 1 | accepted | 1 | 0 | 4.22M | 8.75 |
| refactor-logstats | baseline | 2 | accepted | 2 | 0 | 7.53M | 15.35 |
| refactor-logstats | candidate | 1 | accepted | 2 | 66 | 6.24M | 15.28 |
| refactor-logstats | candidate | 2 | capped_time | 1 | 155 | 4.14M | 4.83 |
| tf-bucket | baseline | 1 | capped_time | 2 | 121 | 4.66M | 10.81 |
| tf-bucket | baseline | 2 | capped_time | 1 | 138 | 2.64M | 6.86 |
| tf-bucket | candidate | 1 | capped_time | 1 | 146 | 2.90M | 3.51 |
| tf-bucket | candidate | 2 | accepted | 2 | 47 | 3.20M | 12.72 |
