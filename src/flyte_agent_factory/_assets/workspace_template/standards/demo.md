# demo

Applies to demo deliverables instead of the production rules they name. Two kinds:

- **Pattern demo**: shows one pattern or feature, and the code is part of what the audience reads. DEMO-001 to DEMO-007 apply, and replace ENG-004 (production test gate), ENG-010 (deployment configuration).
- **Showcase demo**: the full product shown through its UI or command line; the code is not the subject. Production standards apply in full, plus DEMO-006 and DEMO-007.

## DEMO-001 MUST
State which kind the demo is (pattern or showcase) and the one pattern or feature it shows, in the first two lines of its README.

Why: Every other decision follows from the kind and the single subject.

Check: Read the README's first two lines.

Enforced: judgement

## DEMO-002 MUST
In a pattern demo, keep the code to the minimum that shows the pattern: no layers, options, abstractions or error handling beyond what the pattern itself needs.

Why: Anything else competes with the one thing the audience should take away.

Check: For each function, module and option, say how it serves the shown pattern; anything that does not is a finding.

Enforced: judgement

## DEMO-003 MUST
Make the demo do real work on realistic data, not stubs or toy placeholders.

Why: A demo that fakes the work proves nothing.

Check: Run it and confirm it calls the real system and produces a real result; search for mock, fake, dummy, foo and bar.

Enforced: judgement

## DEMO-004 MUST
Give every demo one functional test that runs it end to end and checks the result; unit tests are not required for pattern demos.

Why: A demo that silently breaks is worse than none, but its code is not a library.

Check: Run the documented test command; it executes the demo and asserts on its output.

Enforced: judgement

## DEMO-005 SHOULD
Make a pattern demo readable top to bottom in one sitting: one entry file where possible, names that read as the story, comments only where the pattern needs a why.

Why: The audience reads demo code once, in order.

Check: Read the entry file start to finish; flag any jump to another file that is not essential to the pattern.

Enforced: judgement

## DEMO-006 MUST
Run the demo with one documented command from a clean checkout, with no wrapper scripts.

Why: The command is part of what is being shown.

Check: Follow the README from a clean checkout; the run is a single command line.

Enforced: judgement
Applies: showcase

## DEMO-007 MUST
Keep agent and harness artifacts out of everything the audience sees: names, files, console pages and recordings.

Why: The audience should see the product, not how it was made.

Check: Search the demo folder and its run names for claude, agent, BRIEF.md and CLAUDE.md; review the recording frames.

Enforced: judgement
Applies: showcase
