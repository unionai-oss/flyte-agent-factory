# Writing your own UX standard

## What it is for

A UX standard lets the owner grok what was built without opening it themselves. It states a bar for anything a
person uses or reads, and it states it for the product's own users, not for the people who built it. The reviewers
hold human-facing work to that bar, and they cite evidence: screenshots at stated viewports and a walkthrough of
the main task. Work with no human-facing surface is reported not applicable.

The workspace template ships one, `standards/ux.md`, with a source register, `docs/ux-sources.md`, and the scripts
its mechanical rules run, `standards/checks/`. This page explains how they fit together and how to write your own.

## How the template's standard works

### Only for human-facing work

A standards file whose first line before its rules is

```markdown
applies: human-facing
```

is judged only for human-facing work: apps, prototypes, reports, dashboards, command-line tools, documentation
sites, demos. Its rules reach the reviewers with that condition attached, the same way `brand.md` always has. The
format of the line and of every rule is in [standards.md](standards.md).

### Naming the users: `product.toml`

Every rule is relative to its users, so a project names them in `product.toml` at its root. The template's
audience rule fails until the file is there and valid.

| Table | Key | Meaning |
|---|---|---|
| `[audience]` | `users` | Who they are, in one sentence. |
| | `age_band` | Descriptive text, shown as written in prompts. A range (`"6-8"`, `"13-17"`, `"13+"`), `"65+"` or `"adult"`/`"adults"` is read for its ages; several can be joined with commas or "and". Any other text needs `under_13`. |
| | `under_13` | `true` or `false`: whether children under 13 use the product. Optional when `age_band` is readable, where it must agree with it; required otherwise. A range includes children under 13 when it starts below 13, so `"12-14"` does and `"13-17"` and `"13+"` do not. |
| | `expertise` | `novice`, `intermediate` or `expert`. |
| | `reading_grade` | Highest US school grade of text they read comfortably, an integer from 0 to 18. |
| | `main_task` | The one task a first-time user must finish. |
| `[brand]` | `file` | A path in the project to its own brand file; `"none"` for no brand rules; omitted means the workspace brand applies. |
| `[surfaces]` | `pages` | Project files or http(s) URLs to render, for a product with a UI. |
| | `serve` | Optional shell command, a string, that starts a server before rendering. It is stopped when the script ends, however it ends. |
| | `ready` | http(s) URL polled until it answers, for up to 60 s; required when `serve` is set. |
| | `viewports` | Widths and heights to render at, each side 100 to 10000 px; default `["320x640", "390x844", "1280x800"]`. |
| | `text` | Files (globs) a person reads, such as a report, docs or UI strings; their text is reading-level checked. A product that is only text declares these and no pages. |
| | `cli` | The command a person runs, a string or a list of strings, for a command-line tool. Each is split into words like a shell would (quotes group words; nothing is expanded) and run without a shell from the project directory, with a 20 s limit; the CLI script runs `--help` (which must read as usage), the `cli_invalid` arguments and the `cli_main` arguments on it. It also marks the project as human-facing, and its commands and output belong in the walkthrough. |
| | `cli_invalid` | A list of argument strings the tool must reject, such as `["--no-such-option"]`; required when `cli` is set. The CLI script runs each command with them and expects a non-zero exit and a stderr message that names one of them or points to `--help`. |
| | `cli_main` | A list of argument strings for one representative successful run, such as `["--version"]` or `["list"]`; required when `cli` is set. `[]` is valid and runs the bare command. The CLI script expects exit 0, and fails on an escape code or a carriage return without a newline in the piped output of this run, of `--help` and of the invalid run. |

The engine reads `[audience]` and `[brand]`: the users go into the session's instructions, the contract and every
review prompt, and they override any audience a brand file names. `[surfaces]` is read only by the workspace's
check scripts. Declaring `pages`, `text`, `cli` or an `[audience]` at all makes a project human-facing: declaring users
means someone uses it. Every key must have the type above; a malformed one, such as `surfaces = "x"`, fails each
script with a message naming it. The full reference is in [standards.md](standards.md).

A children's reading game:

```toml
[audience]
users = "Children learning to read, and the parent who sets the game up"
age_band = "6-8"
expertise = "novice"
reading_grade = 2
main_task = "Create a world and walk into its first room"

[brand]
file = "BRAND.md"

[surfaces]
pages = ["dist/index.html"]
serve = "npx --yes serve -l 4173 dist"
ready = "http://localhost:4173/"
viewports = ["320x640", "390x844", "1024x768"]
text = ["src/strings/*.json"]
```

An internal engineering report:

```toml
[audience]
users = "Platform engineers deciding whether to move the build cluster to spot nodes"
age_band = "adult"
expertise = "expert"
reading_grade = 12
main_task = "Read the recommendation and the cost table, and decide whether to approve the move"

[brand]
file = "none"

[surfaces]
pages = ["report/index.html"]
viewports = ["390x844", "1280x800"]
```

The same report shipped as Markdown has no pages: it declares `text = ["report.md"]` instead, and a command-line
tool declares `cli = "mytool"`, `cli_invalid = ["--no-such-option"]` and `cli_main = ["--version"]`. Neither
is rendered, so the render script reports them not applicable; the reading-level and walkthrough scripts
still run, and the CLI script runs the tool.

### Not applicable

A script that exits 0 and whose first non-empty output line starts `NOT APPLICABLE:` is reported not applicable, not as a
pass. The template's scripts do this for a project with no human-facing surface: no `.html`, `.htm`, `.tsx`,
`.jsx`, `.vue` or `.svelte` file, no command-line entry point in `pyproject.toml` (`[project.scripts]`) or
`package.json` (`"bin"`), and no `product.toml` declaring pages, text, a CLI or an audience. A `product.toml` with
only a `[brand]` is not applicable in all four scripts alike. UI files are not looked for in vendored, generated
or test trees: hidden directories (`.git`, `.venv`, `.tox`), `node_modules`, `venv` and any directory holding a
`pyvenv.cfg`, `site-packages`, `__pycache__`, `htmlcov`, `_build` (as in `docs/_build`), `build`, and `tests`,
`test`, `__tests__` and `fixtures`. `dist` is looked in, because a built site there is often the product itself;
a project whose `dist` holds something else declares its pages, or none, in `product.toml`. A project with a UI
file or a command-line entry point but no `product.toml` fails the audience rule until it names its users. The
render script is also not applicable to a project with no pages to render, and the reading-level script to one
with fewer than 100 words of text. The convention is in [standards.md](standards.md).

## Deterministic first

Anything code can measure is measured by a script, so no one's opinion decides it. The template's scripts in
`standards/checks/` measure:

- **The audience.** `product.toml` parses and every `[audience]` field is present and valid; a failure names
  each missing or invalid field, also when `product.toml` itself is missing. A project with UI files also
  declares the pages to render.
- **The walkthrough.** `ux-evidence/WALKTHROUGH.md` exists, names the main task from `product.toml`, and every
  image it links is a PNG that decodes. With pages, it links screenshots at the smallest and largest viewport,
  each as wide as its viewport and at least as tall; a CLI or text product may show a terminal transcript (a
  fenced code block with output in it) or a quoted passage instead. Only relative links
  to files inside the project count, each file once and with any `?query` or `#fragment` ignored: the reviewer
  has the project, not a URL, a `data:` image or a file elsewhere on the machine.
- **The CLI.** Each declared command is run three times, without a shell, from the project directory, with a
  20 s limit each (60 s at most per command): `--help` must exit 0 and read as usage (the word "usage" or an
  option list); the `cli_invalid` arguments must exit non-zero with a stderr message that names one of them or
  points to `--help`; the `cli_main` arguments (`[]` for the bare command) must exit 0. Output is captured as
  bytes from pipes and every stream of all three runs is scanned: any ESC byte (colour or cursor control) or
  a carriage return not followed by a newline (a spinner or progress bar) fails; `\r\n` is fine. The
  environment is the gate's without `NO_COLOR`, `FORCE_COLOR`, `CLICOLOR` and `CLICOLOR_FORCE`, and with
  `TERM=xterm-256color`, so a tool can stay plain only because its output is a pipe.
- **The render.** Each declared page at each viewport, in headless Chromium: console errors and failed requests
  up to about a second after the network goes quiet; horizontal scrolling (reflow), and text cut off at the
  right edge by `overflow-x: hidden` or a fixed box, at the smallest declared viewport and at 320 px when
  declared, with the width checked named in the output; text contrast of at least 4.5:1, or 3:1 for large text,
  including form field values and placeholders, SVG text, `::before` and `::after` generated text (measured
  from its own computed style over the element's backdrop and its own background), `-webkit-text-fill-color`,
  `opacity` and `filter: opacity()`, boxes painted behind text that are not its ancestors, and the dark colour scheme when the
  page styles one; an `alt` on every image; an accessible name on every control, computed as browsers do (a
  hidden label or hidden content names nothing; a `select` or `textarea` needs a label); a page title and a
  language; targets of at least 24 by 24 px, or spaced as WCAG 2.5.8 allows (a link inside a sentence, a
  skip link judged when it has focus, and an unstyled checkbox or radio, whose size the browser sets, are
  exempt); for an audience that includes children under 13, every target at least 44 by 44 px with no spacing
  exception, reported as its own finding (a link inside a sentence is exempt as its size is the text's, a skip
  link is measured when it has focus, and an unstyled checkbox or radio is not exempt); for an `age_band`
  starting at 65 or over, running text (an element with five or more words of its own, outside a button or form
  field) of at least 16 px; every control, native or with an ARIA widget role, reachable by Tab unless it is disabled, inert or
  hidden; and a visible focus indicator, judged by comparing screenshots of the element and 8 px around it with
  and without focus, so an outline, an underline or a ring on the parent all count. Open shadow roots and
  same-origin frames are inspected as part of the page. The keyboard checks press Tab many times, so they run
  at the largest viewport only.
- **Reading level.** The Flesch-Kincaid grade of the rendered pages and declared text files is at most
  `reading_grade` plus one. Pages are rendered in the same browser, so text a script inserts counts, text CSS
  hides does not, and served and URL pages are read too. In Markdown each heading, list item and table row is a
  sentence of its own, link text is kept; fenced and indented code, front matter and HTML comments are left out. A
  file is read once however many patterns match it. It lists the hardest passages. Under 100 words in all,
  counting every page and text file together, the grade is too noisy to mean anything, so the script reports not
  applicable and the reviewer reads the text.

The render script needs `node` and Playwright's Chromium. When either is missing or broken it exits 127, which
the gate reports as tool unavailable: an image defect for the operator to fix, never a pass. The reading-level
script exits 127 the same way whenever pages are declared: it takes page text only from the browser and never
from the HTML source. A page that fails to load or render fails the check and is named, so a partly measured
corpus never passes. A product with only `text` files needs no browser.

Every script checks the `text` entries before it decides anything: each must match a regular file inside the
project, symlinks included, and an entry that matches nothing or leads outside fails in all four scripts,
naming `surfaces.text`.

Some things the render script cannot measure. It says so in its output, under "Not measured", rather than
passing them, and the reviewer judges them in the screenshots: text over a background image or gradient, an
image, video or canvas; text under a CSS filter other than `opacity()` or a blend mode; `::before` and `::after`
content that is positioned out of flow or has a background image or gradient of its own; closed shadow roots and cross-origin frames; text drawn inside images; and console errors later than
about a second after the network goes quiet.

The factory stops a check after 300 s. The render script stays inside that: up to 10 s to find Playwright, 60 s
for `ready` to answer and 200 s to render, 270 s in all. Pages not rendered by the 175th second of rendering are
reported as not rendered, so declare fewer pages or viewports if that happens. The `serve` command runs in its
own process group, which is killed when the script ends, is interrupted or is itself killed.

Evidence goes to `ux-evidence/` in the project: one screenshot per page and viewport, `report.json` with every
finding, and the walkthrough. **Commit it.** Reviewers run in their own containers with no browser, and they judge
the committed screenshots, not the live page.

## Judgement where code cannot decide

Some things only a reader can decide: whether the first screen says what the product is for, whether an error
message says how to fix the problem, whether an empty view says what to do next, whether a chart misleads. Those
rules are `Enforced: judgement`. Each one's `Check:` line names the evidence to cite, almost always a screenshot at
a stated viewport or a step of the walkthrough. A finding with no cited evidence is not a finding.

A scripted rule can carry a `Judged:` line for the part its script cannot see. The reading-level rule is one: the
formula measures sentence and word length, so the reviewer also reads the hardest passages for jargon.

## Writing your own

1. **Start from the template.** Copy `standards/ux.md` and `docs/ux-sources.md`. Delete what does not fit your
   products; add what is missing. Nothing in the engine depends on the template's IDs or wording.
2. **Keep the rule format.** Each rule has a heading with an ID and a level (`MUST` or `SHOULD`), the rule as
   plain text, then `Why:`, `Check:`, `Enforced:`, an optional `Judged:`, and `Sources:`. The engine reads
   `Enforced:`, `Judged:` and `Timeout:`; `Sources:` is for people.
3. **Trace each rule to a source.** A `Sources:` line names keys in your register, and each key in the register
   names a primary source: the paper, the standard, the platform guideline. If you cannot find one, say the rule
   is a house choice.
4. **Prefer a script.** If a rule can be measured, write the script and enforce the rule with it. Keep judgement
   for what code cannot decide, and make its `Check:` line name the evidence.
5. **Keep IDs stable.** Reports and findings name them. Retire an ID rather than reuse it.
6. **Make thresholds come from the audience.** Write "at most the declared reading grade", not "grade 8", so the
   same rule fits a children's game and an expert tool.

### Licences

Write from primary sources, in your own words. Several popular secondary sources cannot be copied freely:

| Source | Licence | What that means for your standard |
|---|---|---|
| lawsofux.com | CC BY-NC-ND 4.0 | No derivatives and no commercial use: link to it, take nothing from it. |
| NN/g, Apple HIG, Material Design | All rights reserved | Cite and link; numbers and heuristic names are facts, prose is not. |
| clig.dev | CC BY-SA 4.0 | Adapted text must be shared under the same licence: paraphrase instead. |
| WCAG and W3C Understanding documents | W3C Document License | Quote with attribution and a link; do not alter normative text. |
| US federal plain-language guidelines | Public domain in the US | Free to reuse. |

## Addenda by product type

The template's rules suit any human-facing product. A workspace that builds one kind of product most of the time
can add rules for it. These are suggestions; each would still need a `Check:`, an `Enforced:` line and sources.

### Web app

- Every route has a unique title, and the browser's back button returns to the previous view without losing work.
- Forms keep what was typed after a validation error, and move focus to the first error.
- A link opens a page and a button runs an action; neither is styled as the other.
- Core Web Vitals are measured on the main page and recorded with the evidence.
- Sessions that expire warn first and offer to extend.

### Mobile app

- Touch targets are at least 44 by 44 pt on Apple platforms and 48 by 48 dp on Android.
- Content stays inside the safe areas: no controls under the notch, the status bar or the home indicator.
- The app shows a clear offline state, keeps what the user entered, and retries when the network returns.
- The system back gesture does what the user expects and never silently discards work.
- Text follows the system text-size setting.
- Screenshots are taken on at least one small and one large device size.

### Command-line tool

- `--help` prints usage with at least one example and exits 0.
- Exit codes are 0 for success and non-zero for failure; errors go to stderr and say what to do.
- No colour, spinners or progress bars when output is not a terminal, or when the `NO_COLOR` environment variable
  is set.
- Output a script would parse is available as `--json` (or another stable format) and keeps its shape between
  versions.
- A destructive command asks for confirmation on a terminal and takes a flag to skip it in scripts.

### Report or document

- It opens with the answer or main finding, then what it is and who it is for, then what to do next.
- Terms the audience may not know are explained where they first appear, or in a glossary.
- Every chart has a title, labelled axes with units, and a source; bar and area charts start at zero.
- Every number states its unit, period and where it came from.
- Headings let a reader skim to the part they need.

### Dashboard

- Each panel answers one question, written as its title.
- Every panel shows when its data was last updated.
- Empty and stale states are distinct from zero, and say what to do.
- Every value carries its unit; comparisons state their baseline.
- The most important panel is first and largest.

### Children's product

- `product.toml` names a narrow age band, and every rule is judged against it.
- Core navigation works without reading: icons, pictures and read-aloud text.
- No adverts, no dark patterns and no collection of personal data.
- Targets are large (at least 44 px), with no double-clicks, fine drags or time pressure.
- Anything that leaves the product, buys something or changes settings sits behind a parental gate.
- Every screen has an obvious way back.

### Developer tool or API

- Every error says what went wrong and how to fix it, with the failing value.
- Every example in the docs is complete and can be copied and run as written.
- Names are consistent across endpoints, flags and fields: one word for one thing.
- Defaults are safe, and changing behaviour between versions is announced before it happens.
- The quickest path to a first working call is on the first page of the docs.

The register cites research behind these: developers make predictable errors (Myers and Stylos, 2016) and do read
error messages (Barik et al., 2017).

## Further reading

- The template's source register: `docs/ux-sources.md` in the workspace template.
- W3C, Web Content Accessibility Guidelines 2.2: https://www.w3.org/TR/WCAG22/
- Command Line Interface Guidelines: https://clig.dev/
- web.dev, Web Vitals: https://web.dev/articles/vitals
- Federal Plain Language Guidelines: https://digital.gov/resources/federal-plain-language-guidelines
- Hourcade, Child-Computer Interaction: https://homepage.cs.uiowa.edu/~hourcade/book/
- https://lawsofux.com
