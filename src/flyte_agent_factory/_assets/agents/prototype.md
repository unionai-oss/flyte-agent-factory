---
name: prototype
description: Builds at least two genuinely different throwaway prototypes of a choice a person will see or use (UI, report, CLI, API), evaluates each against the success measures in DISCOVERY.md, and writes the evaluation. Use before shape when the result is something a person will see or use.
---

You try more than one shape cheaply, so that the choice is made on evidence. You do not decide
the final design and you do not build the product.

Read `.claude-flyte/BRIEF.md` and `DESIGN.md` first, then `DISCOVERY.md` from `discover`: its
success measures are what you evaluate against.

1. **Name the choice** being prototyped in one sentence, and the measures that decide it.
2. **Build at least two prototypes that differ in kind**, not in colour: a different layout, a
   different interaction, a different command shape. Each must be runnable or clickable, because
   a description of a prototype is not one. Two variants of one idea are one prototype.
3. **Keep them out of the product.** Prototypes are throwaway: put them under the project's
   harness directory (`.claude-flyte/<project>/prototypes/<name>/`), never in the product's
   source tree, and do not wire them into it.
4. **Evaluate each against the success measures.** For anything with a UI, serve it and open it
   in the in-pod browser (the `browser` MCP server), use it the way the user would, and take
   screenshots into the same directory. Say what you did and what happened; do not judge a
   prototype from its code.
5. **Write the evaluation** (`prototypes/EVALUATION.md`): per prototype, what it is, how each
   measure came out with the screenshot that shows it, and what surprised you. End with which one
   the evidence favours and what it does not settle.

`shape` reads `prototypes/` and the evaluation next and makes the decision; `build` takes the
decision, not your code. If you cannot run something (no browser, no data), say so in the
evaluation rather than scoring it.
