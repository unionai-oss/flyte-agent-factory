# Demo: how many labelled images do you actually need?

Re-record and repackage the work in `image-labels/`. There is an existing
package and video — replace them. The numbers are settled and in `REPORT.md`;
do not re-run any training.

## Audience

A **data science or ML manager** about to commission an annotation budget.
They are accountable for a number on a spreadsheet — how many images get
labelled, at what cost per label — and for how long it takes before a model
is usable. While this plays they are deciding whether to change that number.

Put that line in the manifest as `audience`.

## Subject, and the example

**Subject: on Flyte, the expensive question gets answered by running the
experiment, not by arguing about it.** Fan out the sweep, each arm in its own
container with its own logs, read the curve, decide. That is the capability.

**Example: a label-budget curve on Imagenette.** Ten labelled images per class
with a pretrained backbone reaches **86.1%**; training from scratch on the
*entire* dataset reaches **85.6%** (~947 per class). Ten images ≈ the whole
dataset.

Both are worth saying, and in that order of emphasis the result leads — it is
the hook — but the close returns to the subject: this took one sweep, and so
does the next question.

Put the subject line in the manifest as `subject`.

## Four beats, 30–60 seconds

1. **The question.** A team is about to pay for labelling. How many do they
   need? The two numbers, nothing else on screen.
2. **The curve.** `label_budget_curve.png` — both arms, seed spread. Where
   pretrained flattens, and that from-scratch never does.
3. **How the answer got made.** Five budgets × two arms × three seeds = 30
   containers, concurrent, each with its own logs. Show the child actions in
   the Union console.
4. **The caveat, said plainly.** Imagenette's ten classes *are* ImageNet
   classes, so the backbone has seen them; on a genuinely new domain expect a
   smaller advantage. `REPORT.md` §4 has the wording.

Beat 4 is not a disclaimer to bury. An engineer thinks of it within seconds,
and saying it first is what makes the other numbers credible.

## Start from the code that is already there

`demo/image-labels/` already holds the trimmed subset: `train_one.py`,
`imagenette.py`, `requirements.txt`, `build.sh`, and the three plots. Start
from those rather than from `image-labels/`.

`train_one.py` is the thing to turn into the Flyte task you film.

**Re-run the sweep for the footage.** The earlier brief said not to re-run the
training; that was about not disturbing settled numbers, and it conflicts with
filming a run of your own. Resolve it this way: the sweep is five budgets × two
arms × three seeds and each arm took two to eight minutes, so a fresh run is
tens of minutes of cheap CPU — run it. Then the footage and the numbers come
from the same run, which is the honest version of this demo.

If the fresh numbers differ slightly from `REPORT.md`, quote the fresh ones and
update `REPORT.md`, rather than filming one run and narrating another. If they
differ *a lot*, stop and say so with `flyte-note` — that is a finding, not a
rounding error.

Also note: `assets/audio/` is missing from the existing package (a `.gitignore`
rule ate it), so the narration has to be generated fresh regardless.

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
- **The deliverable was WebM only.** It must be MP4/H.264.
- **`assets/audio/` never made it into the package** — a `.gitignore` rule ate
  it, so the music and every narration line were unrecoverable. Confirm with
  `git ls-files` that every asset the manifest names is actually committed.
- **Narration read the slides.** Rewrite so each line is worth hearing with
  the slide hidden — the slide says *86.1% from ten per class*, the narration
  says what that does to the annotation budget.

## What goes in the package

`demo/image-labels/`: README, the trimmed runnable subset (`train_one.py` and
what it needs — not the fan-out launcher or the 30-run aggregation), the three
plots, and the MP4. Everything the video was composed from under `assets/`,
with the manifest naming all of it.

Narrate with the voice model you are given. Follow `BRAND.md`.
