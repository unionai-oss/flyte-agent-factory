# Demo: learning a polynomial from raw `x`

Package and record the work in `polynomial/` for someone outside the company —
a prospective customer, or an engineer evaluating Flyte who has never seen this
repo.

## What the demo should land

One idea, in this order:

1. **The constraint is the interesting part.** The model sees a single raw
   scalar `x` and nothing else: no powers, no basis functions, no hand-built
   features. Say that plainly up front, because without it the result looks
   trivial.
2. **In range it works.** RMSE ~0.50 against a mean baseline of 18.57 — a 37×
   reduction, and ~0.05 against the noise-free curve.
3. **Outside the training range it fails completely.** ~3,600 RMSE at
   4<|x|<6. Show the plot where the prediction leaves the curve. Do not
   soften this: it is a real and well-known property of function
   approximators, and showing it honestly is more convincing than hiding it.
4. **It ran on Flyte.** Training was a child action with its own container and
   logs. Show the run in the Union console — the console is reachable with the
   pod's own API key, see your project memory.

Aim for 60–90 seconds. Four chapter cards, one per section above.

## What goes in the package

The trimmed, runnable subset — not the research directory. A reader should be
able to open the folder, read the README, and run one command.

Keep: the model and training code, the evaluation, the plot, and whatever is
needed to reproduce the headline numbers on a laptop.

Leave out: the 450KB `results.json`, the post-hoc analysis scripts, the
multi-seed sweep machinery, and the child-action launcher. Mention in the
README that the full sweep lives in the repo, and move on.

The README should state the polynomial, the constraint, the headline numbers,
and the honest limitation — in a form someone can skim in thirty seconds.

## What not to do

- Do not re-run the training or try to improve the result. The numbers are
  settled; they are in `polynomial/REPORT.md`.
- Do not show a terminal installing dependencies. Nobody is persuaded by a
  progress bar.
- Do not claim the model learned the polynomial. It learned the curve on
  [-2, 2]. That distinction is the point of section 3.
