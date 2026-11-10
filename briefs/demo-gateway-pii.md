# Demo: the Union LLM Gateway, with a pluggable policy in front of it

Re-record and repackage the work in `gateway-pii/`. There is an existing
package and video — replace them. Read `demo/gateway-pii/README.md` first;
the numbers are settled and you are not re-running anything.

## Audience

A **platform lead** who owns how their company's teams reach LLMs. They are
accountable for cost, for which data leaves the building, and for not being
the bottleneck when ten teams want ten different models. While this plays they
are deciding whether to spend an hour trialling it.

Put that line in the manifest as `audience`.

## Subject, and the example

**Subject: a fast, pluggable, scalable LLM gateway.** Every call in the org
through one endpoint and one key, with policy *you* own running in front of
the model, and nothing in the path that makes it slower or caps how much of it
you can run. That is the thing to be remembered.

**Example: PII detection choosing the route.** Clean prompt → Anthropic; a
prompt with personal data → self-hosted Qwen in your own cluster.

Keep these straight, because the last cut did not. **PII detection is not a
feature of the gateway.** It is one implementation of a policy stage, chosen
here because it is legible in ten seconds — there are infinite others: cost
caps, prompt rewriting, tenant isolation, a classifier, an approval step. A
viewer should finish thinking *"I could put my own policy there"*, not *"this
product does PII"*. Say the subject at the open and again at the close, and
let the example carry the middle.

Put the subject line in the manifest as `subject`.

## Four beats, ~30 seconds

1. **Their problem.** Teams want the best model; some prompts hold customer
   data that cannot go to a third party; today that is solved with a wiki page
   and hope.
2. **One endpoint, one key, policy in front.** The architecture in a single
   slide — this is the subject, state it plainly.
3. **The example, on screen.** The chat app: clean message → Anthropic, message
   with a name and a card number → Qwen, route visible both times. Most of the
   time goes here, because this is the beat that makes beat 2 believable.
4. **Fast, and it scales.** Your measured numbers, then the guardrail fanning
   out as 30 concurrent containers. Close by returning to the subject: the
   stage you just watched is yours to replace.

## On speed and scale, precisely

- **Speed:** quote your own in-cluster measurement — guardrail 5.6 ms median,
  gateway overhead under 30 ms against direct-to-Anthropic. Give the caveat in
  the same breath (two runs, +26 ms and −10 ms, i.e. noise).
- **Scale:** the honest claim is about the architecture. Flyte apps take
  `flyte.app.Scaling(replicas=(min, max))` and tasks fan out — which is why the
  guardrail runs 30-wide. **The gateway deployment itself runs one replica
  today by configuration**, because its budget counters are in-process. Say
  "scalable by design", show the fan-out as the evidence, and do not imply the
  gateway is currently autoscaling.

## Start from the code that is already there

`demo/gateway-pii/` already holds a trimmed, runnable subset, and it is your
starting point for *both* halves of this job — the package you ship and the
Flyte task you film. Read it; fix what is wrong; do not rewrite it from
`gateway-pii/`.

| file | what it is |
|---|---|
| `guardrail.py` | Presidio analysis, returns the route decision |
| `routing.py` | picks the model, talks to the gateway |
| `chat_app.py` | the UI that makes the route visible |
| `fanout.py` | the 30-way guardrail fan-out |
| `testset.jsonl` | 30 synthetic messages with expected routes |
| `bench.py`, `bench_results.json` | the latency measurement |

Two things you need to know about it:

- **`fanout.py` was rewritten and has not been run in that form.** It used to
  import the agent harness, which a recipient cannot have; it is now ordinary
  Flyte — a task in its own environment, driven by `flyte.init_from_config()` +
  `flyte.run`, with the guardrail source and test set passed in as inputs.
  Running it is both how you verify it and how you get the fan-out footage.
  If it does not work, fix it: it ships in the package.
- **Re-running the fan-out is expected.** The last one took 17 s for 30
  containers, so there is no reason to film a stale run. A fresh run also has
  no failed history on screen, which is the specific thing that spoiled the
  last cut.

The benchmark numbers in `bench_results.json` are settled — do not re-measure
them, and quote them as they are.

## The run you film

The work in this repo already ran, under the agent's own supervisor run — so
that run is unusable as footage: the console header reads
`claude-agent.project`, with a multi-day parent and an *Abort run* button. Its
child actions are no better; they render under the same header. Two demos
shipped like that.

So make a run worth filming:

1. Take the trimmed, runnable code you are putting in the package and make it a
   Flyte task.
2. Name its `TaskEnvironment` for the work — that name is the biggest text on
   the console page.
3. Run it **without** `attach()`. That is the whole trick: `attach()` is what
   makes work a child of this run, so omitting it gives you a clean top-level
   run of your own.
4. Film that run.

The code that produces the footage ships in the package, which is the point —
a recipient can run the same thing and see the same screen.

## Fix these from the last cut

- **The console shot showed the agent's supervisor run.** See *The run you
  film* above — make a clean run and film that.
- **`11 Failed (11.5%)` was on screen** under narration claiming zero
  failures. A fresh run has no failed history to show.
- **Narration read the slides.** Rewrite it so each line is worth hearing with
  the slide hidden.
- Keep only the MP4 in the package root; the WebM belongs under `assets/` as
  the source capture, not beside the deliverable.

## What goes in the package

`demo/gateway-pii/`: README, the runnable subset (guardrail, routing, chat app,
`fanout.py`), the latency table, and the MP4. Everything the video was composed
from under `assets/` — slides, screen capture, **and `assets/audio/`**, one wav
per line — with the manifest naming all of it, so the music or one line can be
changed without re-recording.

Narrate with the voice model you are given. Follow `BRAND.md`.

## What not to do

- No real PII; synthetic and obviously so.
- Do not print either API key, or show a terminal where one is visible.
- Do not re-run the training, the test set or the benchmark.
