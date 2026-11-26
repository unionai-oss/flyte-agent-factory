# ux

applies: human-facing

**What this is.** The bar for anything a person uses or reads: apps, prototypes, reports, dashboards, CLIs,
docs sites. **How to use it.** Name your users in `product.toml` at the project root (UX-001); everything
else is judged against those users. Scripts in `checks/` measure what code can measure (contrast, layout at
three widths, console errors, reading level) and write screenshots to `ux-evidence/`; the reviewers judge the
rest from that evidence and a written walkthrough of the main task. **Where the rules come from.** Each rule
lists its sources by key; `docs/ux-sources.md` at the workspace root traces every key to its primary source
and says how it is checked. A project with no human-facing surface is reported not applicable.

Ages, grades and widths below come from the declared `[audience]`: "the audience" always means those users,
not the people building the product.

## UX-001 MUST
Name the users in `product.toml` at the project root: who they are (`users`), `age_band`, `expertise`
(novice, intermediate or expert), the highest school grade of text they can read comfortably
(`reading_grade`), and the one task a first-time user must finish (`main_task`). An `age_band` that is not
a range, `65+` or `adult` also needs `under_13 = true` or `false`, saying whether children under 13 use it
(see UX-021). A project with a UI also lists the pages to render (`[surfaces] pages`);
a report or other text product lists the files a person
reads (`[surfaces] text`), and a command-line tool the command a person runs (`[surfaces] cli`). UI files
or a command-line entry point in the packaging make a project human-facing, so it needs the file. Its own
brand, or `none`, goes in `[brand] file`; without it the workspace brand applies.

Why: Every other rule is relative to its users; a product designed for "everyone" is judged against its
builders, who already know how it works.

Check: Run the audience script: the file parses and every field is present and valid.

Sources: usability-testing-small-samples, children-age-bands, mental-model

Enforced: script checks/ux_audience.py

## UX-002 MUST
A first-time user from the audience can tell, from the first screen alone, what the product is for and
what to do first to start the main task, without reading separate documentation.

Why: People start using a product before they read about it, and leave when its purpose is unclear.

Check: From the first screenshot at the smallest viewport, state in one sentence what the product is for and
which control starts the main task. If a reviewer cannot, or needs text outside the product, it fails; cite
the screenshot.

Sources: paradox-active-user, nielsen-heuristics, mental-model

Enforced: judgement

## UX-003 MUST
Commit a walkthrough of the main task, `ux-evidence/WALKTHROUGH.md`: each step a first-time user takes, as
that user, with a screenshot per step at the smallest and largest viewport, or for a CLI or text product
the command and output or passage the user sees, the result they see, and every point where they could get
stuck. It ends with the completion state and one failure path.

Why: A task that was never walked end to end is only assumed to work; screenshots let a reviewer without a
browser see what the user sees.

Check: Run the walkthrough script: the file exists, names the main task from `product.toml`, and every image
it links is a PNG that decodes; with pages, it links screenshots at the smallest and largest viewport, each as
wide as its viewport and at least as tall, and without, it shows a terminal transcript (a fenced block with
output in it), a quoted passage or a screenshot. The reviewer then judges
whether the steps reach the task's end and are honest about the stuck points.

Judged: whether the walkthrough completes the main task as a member of the audience would attempt it, and
whether its screenshots match its words.

Sources: cognitive-walkthrough, usability-testing-small-samples, peak-end-rule

Enforced: script checks/ux_walkthrough.py

## UX-004 MUST
Every declared page renders cleanly and accessibly at 320, 390 and 1280 CSS px wide (or the declared
viewports): no console errors or failed requests; no horizontal scrolling at the smallest viewport (320 px by
default); text contrast at least 4.5:1 (3:1 for large text); every image has an `alt` (empty only when
decorative); every control has an accessible name; the page has a title and a language; every target is at
least 24 by 24 px or spaced as WCAG 2.5.8 allows; every control can be reached by Tab and shows a visible
focus indicator.

Why: These are the failures users hit first and code can find without anyone's opinion; the screenshots
it writes are the evidence for the rules a reviewer judges.

Check: Run the render script; it writes `ux-evidence/<page>@<width>x<height>.png` and `report.json`, and
lists each finding with its selector, page and viewport. It checks reflow at the smallest declared width
(and at 320 px when declared) and says which, and it presses Tab for the keyboard checks at the largest
viewport only. A project with no pages to render is not applicable here.

Judged: whether the focus order follows the visual order, and whether anything the script lists as not
measured (such as text over images or gradients, positioned generated text, closed shadow roots or
cross-origin frames) is readable and usable in the screenshots.

Sources: wcag-1.4.3, wcag-1.4.10, wcag-1.1.1, wcag-4.1.2, wcag-2.4.2, wcag-3.1.1, wcag-2.5.8, wcag-2.1.1,
wcag-2.4.7, fitts-law

Enforced: script checks/ux_render.py

## UX-005 MUST
Text the audience must read is at or below their declared reading grade: short sentences, common words,
the main message first.

Why: Text above a reader's level is skipped or misread, and for young readers it blocks the task entirely.

Check: Run the reading-level script: the Flesch-Kincaid grade of the rendered pages and declared text files is
at most `reading_grade` plus one, and it lists the hardest passages. Under 100 words in all, counting every
page and text file together, the grade is too noisy to measure: the script reports not applicable and the
reviewer reads the text.

Judged: a formula measures sentence and word length, not meaning; the reviewer also checks the hardest
passages for jargon the audience would not know.

Sources: flesch-kincaid, readability-formula-limits, plain-language-federal, plain-language-iso

Enforced: script checks/ux_reading_level.py

## UX-006 MUST
Every action that can fail says, in the audience's words, what went wrong and how to fix it, next to where
it happened; it keeps what the user entered and never shows a stack trace, raw error code or blank screen.

Why: Errors are where users give up; a message they cannot act on ends the task.

Check: For each input and each network or file action, cite a screenshot of its error state (the walkthrough's
failure path counts for one). An action with a failure the product does not show is a finding.

Sources: wcag-3.3.1, error-messages, nielsen-heuristics, robustness-principle

Enforced: judgement

## UX-007 MUST
Every view that can be empty (no data yet, no results, nothing selected, first run) says why it is empty and
what the user can do next, with the control to do it when there is one.

Why: A blank area reads as broken, and first use is almost always empty.

Check: List every list, table, chart and search in the change; for each, cite a screenshot of its empty
state. A bare "No data", or an empty area, is a finding.

Sources: empty-states, nielsen-heuristics

Enforced: judgement

## UX-008 MUST
Give feedback for every action: a visible response within 0.1 s, a busy indicator for anything over 1 s, and
progress with a way to cancel for anything that can take over 10 s.

Why: Without a response people repeat the action or assume it failed; the thresholds are where attention
and the sense of direct control break.

Check: For each action in the walkthrough that waits on work, cite its busy or progress state; a wait with no
indication is a finding.

Sources: response-time-limits, doherty-threshold, nielsen-heuristics

Enforced: judgement

## UX-009 MUST
Follow the conventions the audience already knows from the platform and from products like this one:
standard controls, familiar words and icons, expected placement. Any deviation is stated in DESIGN.md with
its reason.

Why: Users spend most of their time in other products, and bring those expectations with them.

Check: List each non-standard control, gesture, label or placement in the screenshots; one without a stated
reason in DESIGN.md is a finding.

Sources: jakobs-law, mental-model, apple-hig, material-design

Enforced: judgement

## UX-010 MUST
Each view has one most important action, and it is the most prominent thing on it; choices are few, with
a sensible default or a recommendation wherever more than a handful appear.

Why: Every extra option adds decision time, and when everything is emphasised nothing is.

Check: For each screenshot, name the primary action; two equally prominent primary actions, or a long set of
choices with no default or recommendation, is a finding.

Sources: von-restorff, hick-hyman, choice-overload

Enforced: judgement

## UX-011 MUST
Never make users carry information from one step to the next in their heads: show it where it is needed,
offer a copy control for codes and identifiers, and group long numbers and keys.

Why: Short-term memory holds only a few items, and anything held there is lost on interruption.

Check: Walk the main task and list every value a user must remember or retype between steps; each is a
finding.

Sources: working-memory, chunking, cognitive-load

Enforced: judgement

## UX-012 MUST
A task of three or more steps shows where the user is (step n of N or a progress indicator), lets them go
back without losing work, and keeps partial work when interrupted.

Why: People persist when they can see the end, and abandon tasks that punish a pause.

Check: In the walkthrough, cite the progress indicator; go back one step and leave mid-task, then report what
survived.

Sources: goal-gradient, zeigarnik, nielsen-heuristics

Enforced: judgement

## UX-013 MUST
A report, dashboard or other output meant to be read opens with what it is, who it is for, the answer or
main finding, and what to do about it, before any detail; every chart labels its axes, units and source,
and a bar or area chart starts at zero.

Why: Readers decide in the first screen whether to keep reading, and a chart without labels or with a
truncated baseline misleads.

Check: Read only the first screen: state what it is, its finding and the next action. Then inspect every
chart for labels, units, source and baseline.

Sources: plain-language-federal, data-ink-integrity, graphical-perception

Enforced: judgement

## UX-014 MUST
A command-line tool prints usage for `--help` and exits 0; given input it cannot accept, it exits non-zero
and writes a message to stderr that names the rejected input or points to `--help`; and when its output is
not a terminal it writes no colour or progress animation.

Why: A CLI's users are people at a terminal and the scripts they write; both rely on the exit code and on
help being there when asked, and escape codes and spinners corrupt what a script captures.

Check: Run the CLI script: for each declared command it runs three invocations with the output piped.
`--help` exits 0 and identifies itself as usage (the word "usage" or an option list); the declared invalid
invocation (`[surfaces] cli_invalid`) exits non-zero with a stderr message that names an argument from
`cli_invalid` or points to `--help`; the declared main invocation (`[surfaces] cli_main`, one representative
successful run, `[]` for the bare command) exits 0. Stdout and stderr of all three are scanned as bytes: any
ESC byte, or a carriage return not followed by a newline, fails. The environment is cleared of `NO_COLOR`,
`FORCE_COLOR`, `CLICOLOR` and `CLICOLOR_FORCE` and sets `TERM=xterm-256color`, so only the pipe can keep the
output plain. Whether the help is enough to use the tool is still judged.

Sources: command-line-guidelines, developers-as-users, developer-error-messages, nielsen-heuristics

Enforced: script checks/ux_cli.py

## UX-015 MUST
Fit the product to the declared age band and expertise. For children under 13: core navigation does not
depend on reading, there are no adverts, dark patterns or collection of personal data, and every screen has a
way back. For older adults: no time limits without a way to extend them. For experts: shortcuts for repeated
actions and no forced tutorials. The sizes of targets and body text for these ages are measured by UX-021.

Why: Motor control, reading and tolerance for friction differ sharply by age and experience; a design for
one group can exclude another.

Check: Compare the screenshots with the declared band and expertise, line by line above; cite each mismatch.

Sources: children-age-bands, child-computer-interaction, older-adults

Enforced: judgement

## UX-016 MUST
Write in plain language for the audience: the words they use, active voice, one idea per sentence, and no
unexplained jargon, acronyms or internal names.

Why: Users read in fragments; anything they must decode slows them or sends them away.

Check: Quote each term the audience would not know and each sentence that needs rereading; each is a finding.

Sources: plain-language-federal, plain-language-iso, mental-model

Enforced: judgement

## UX-017 SHOULD
Meet Google's "good" Core Web Vitals on a mid-range phone profile: largest contentful paint within 2.5 s,
interaction to next paint within 200 ms, layout shift under 0.1.

Why: Slow and jumping pages lose users before they start the task.

Check: Run a lab measurement on the main page and record the three values.

Sources: core-web-vitals, response-time-limits

Enforced: judgement

## UX-018 SHOULD
Group by meaning: related things sit close together or share a container, unrelated things are visibly
apart, and items that look alike behave alike.

Why: People read layout before words; spacing and styling that contradict meaning cause misreads.

Check: In the screenshots, point to any label nearer a neighbouring control than its own, any container
holding unrelated items, and any non-interactive text styled as a link.

Sources: gestalt-grouping, common-region

Enforced: judgement

## UX-019 SHOULD
End every task on a clear completion state: what happened, where the result is, and what to do next.

Why: The end of an experience weighs most in how people remember it.

Check: Cite the completion screenshot from the walkthrough.

Sources: peak-end-rule, nielsen-heuristics

Enforced: judgement

## UX-020 SHOULD
Keep the visual design tidy and consistent, but do not take attractive screens as evidence of usability.

Why: People rate attractive designs as easier to use, which hides real problems in reviews and tests.

Check: Every usability claim in the walkthrough rests on a task outcome, not on how the screens look.

Sources: aesthetic-usability

Enforced: judgement

## UX-021 MUST
Size targets and text for the declared ages. When the audience includes children under 13, every target a
person operates is at least 44 by 44 CSS px, with no spacing exception. When it is for older adults (an
`age_band` range starting at 65 or later, such as `65+`), running text is at least 16 px. Whether children
under 13 are included comes from `age_band` when it is a range (`6-8`, `10-14`, `13+`), `65+` or `adult`,
and otherwise from `under_13 = true` or `false` in `[audience]`; the audience script fails when that is
missing or contradicts the range.

Why: Small hands, and fingers and eyes that have aged, miss targets and text that suit a young adult; the
general 24 px minimum in UX-004 is too small for them.

Check: Run the render script; it lists each target under 44 by 44 px for a children's audience, and each block
of running text under 16 px for an older one, with its selector, page and viewport. A link inside a sentence
is exempt, because the text around it sets its size. An unstyled browser checkbox or radio is not exempt for
children, who need it enlarged. A link visually hidden until focused (a skip link) is measured while focused.
Running text is an element with at least five words of its own, outside a button or form field; headings and
labels are not measured. The script reads the age from `product.toml` and measures nothing for other ages.

Sources: wcag-2.5.8, children-age-bands, child-computer-interaction, older-adults, apple-hig

Enforced: script checks/ux_render.py

## UX-022 MUST
A command-line tool's error messages say what the person should do next, and anything a script would parse
is available in a stable format.

Why: A message that only says something went wrong leaves the person guessing, and scripts break when the
shape of the output shifts.

Check: Read the message each bad argument produces and say what to do next; name the stable format (JSON,
one record per line) a script can parse. Code cannot decide these. The deterministic proxy for "what to do
next" (the message names the input or points to `--help`) is already tested by the CLI script's stderr test.
Which output a script would parse, and whether its shape stays stable between versions, is for the reviewer
to judge: nothing in `product.toml` can name a future version.

Sources: command-line-guidelines, developers-as-users, developer-error-messages, nielsen-heuristics

Enforced: judgement
