# Judgement: who decides a question

A project asks with `ask-human`. Some questions are the owner's to answer. Many are not: "which of these two
designs?", "may I skip this test?", "can you waive this check?" have a defensible right answer, and sending them
to the owner with a couple of bad options and a default chosen to cover silence only moves the work onto a person.
The owner put it this way:

> This isn't something that I think should make it up to me, it should be answered by "what is the right thing
> to do".

and, about a request for waivers on checks that failed because of the gate's own environment:

> Are waivers from running checks? No. Fix things.

So the factory classifies every question before it reaches the owner, and decides the engineering ones itself.

## How a question is routed

```text
ask-human "<question>"
   │
   ▼
judge sub-agent (fresh context: workspace snapshot, brief, the owner's direction so far,
                 standing owner rules, the standards)
   │
   ├── owner-reserved ──────────────► Flyte condition `ask-…`, one at a time, with the judge's
   │                                   reason and a recommendation appended
   ├── unsure / failed / cannot run ─► same as owner-reserved (the safe direction)
   └── engineering judgement ───────► decision-NNN, answered straight back to the session
```

`ask-human --browser` approvals always go to the owner. Time spent judging does not count against the ask's
timeout. An engineering decision must carry its reasoning: one without it is never recorded or delivered, and
the question goes to the owner as a judge that failed. When the bridge stops, a judge still deciding is
cancelled and awaited with it.

## The owner-reserved floor

These are always the owner's:

- spending money, or paid or API-key usage;
- credentials, secrets, access grants or sign-ins;
- irreversible or external actions;
- publishing, or anything outward-facing;
- merging outside the factory's sandbox;
- product direction or scope the owner has not set;
- a report of finished work, or a request for an action only a human can take;
- anything the owner has explicitly kept for themself.

A deployment profile can add to this list; it cannot remove anything from it. The session's asking guidance, the
judge and the operator chat's instructions all render this one list, with the profile's additions, so an added
category reaches the question wherever it is first classified. The `[judgement]` profile section is documented
below.

## Engineering judgement

Design, structure, refactoring, how to satisfy a check, test strategy and tooling are decided by the judge as
"what is the right thing to do". It judges against the owner's standing direction, the brief, the target repo's
conventions and the factory's standards. It is not limited to the options the asker listed and is not swayed by
the asker's stated default.

### No waivers

A waiver, a lowered threshold, a removed rule or a skipped test is never the decision. Two layers keep it so:

- **The judge's declaration.** Every engineering answer carries `WAIVER: yes | no`: yes if the decision makes a
  check pass by waiving it, moving a threshold or limit, exempting something from a rule, removing or disabling
  a rule or check, or skipping, deleting or weakening a test. A missing field counts as not declared.
- **A phrase guard**, the backstop for a declaration that is missing or wrong. It matches wordings such as
  "add a waiver", "raise the limit", "exclude the file from the check" or "skip the test". A negation cancels a
  match only when it governs it, within about three words right before it ("no waiver", "rather than waiving");
  "Rather than rewriting the module, add a waiver" is still flagged. It errs towards flagging.

A decision flagged by either layer is rejected once, naming the phrase the guard matched, and the judge decides
again and states `WAIVER:` honestly. The guard is final: if the second answer still declares a waiver or the
guard still matches it, whatever the judge declares, or the judge fails, the decision becomes the standing rule
itself (fix the code or the check) and its reasoning names the phrase the guard matched.
When a check is wrong, the decision is to fix the check. Sessions are told the same in their standing
instructions: fix the code or the check, never ask for a waiver to get a check to pass.

## The `[judgement]` profile section

Optional. Both keys are lists of strings.

```toml
[judgement]
# Extra categories that are always the owner's, on top of the floor above.
owner_reserved = ["changes to the public API of the billing service"]
# Standing direction the judge applies to engineering decisions.
standing_direction = ["prefer the standard library to a new dependency"]
```

## What is recorded where

An engineering decision gets an id `decision-NNN`. No owner question is opened. It appears:

- in the session, as `ask-human` output: `Decided in the factory (decision-NNN, engineering judgement — not
  escalated to the owner): …` and `Why: …`, exit 0. For `--approve`, approved or declined exits 0 or 1; a
  decision that is neither is given in words and exits 4;
- in the timeline, as a ⚖️ row;
- in the dashboard, under "Decided in the factory";
- in `factory.status`, under `decisions`, with its reasoning.

Sessions also record their own calls with `flyte-note "DECISION: …"`.

## Overruling a decision

Send ordinary direction that names the id: a Union `direct-` message or a message in the session, for example

```text
overrule decision-003: split the module instead
```

A sentence that names the id and says it is overruled marks the decision overruled in the timeline and in
`factory.status`. That is one of the verbs overrule, override, reverse, revert, undo, reject, withdraw, cancel,
scrap or drop, or the phrasings "don't follow decision-003" and "decision-003 was wrong". A negation in the two
words before the verb voids it ("no need to undo decision-003"). A sentence that only names the decision
("decision-003 was right, do not change it") is shown as a reference (📎) and changes nothing. A short message
typed in the Claude app, such as "Overrule decision-003.", overrules too. The session receives the direction as
usual and must act on it.

## Operator chats

A chat connected to the factory MCP follows the same rule. It answers an engineering-judgement `ask-` itself
(what is the right thing to do, never a waiver), signalling a value that starts
`Operator decision (engineering judgement): …` with the reasoning, and tells the user what it decided. It relays
only owner-reserved questions to the user, one at a time, stated plainly with a recommendation, and signals the
user's answer verbatim.

## Replaying recorded questions

To check the judge against questions seen in practice, record each as a fixture and replay it:

```bash
python -m flyte_agent_factory._judgement replay <fixture.json> [...] [--model M] [--engine claude|delegate]
```

```json
{
  "name": "waiver-for-environment-failure",
  "question": "…",
  "kind": "text",
  "brief": "…",
  "directions": ["…"],
  "context": "…",
  "expect": {"class": "engineering"}
}
```

The replay builds the production prompt, with `context` appended as what the workspace shows, and applies the
same parsing and waiver guard as the supervisor. `--engine claude` (the default) runs it through the local
`claude -p`, in an empty directory. `--engine delegate` hands it to a `delegate --answer` worker, for a project
pod where the local CLI is not signed in. It prints each call and compares the class with `expect.class`. A
judge that did not run fails, even when the owner fallback happens to match. Recorded questions are kept in
`tests/fixtures/judgement/`.

Results of the last live replay are in [judgement-replays.md](judgement-replays.md).
