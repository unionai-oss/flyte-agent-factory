# brand

Applies only to visual or customer-facing deliverables: UI, docs sites, demos, reports and slides.

Source: Union's "Union / Flyte color palette" (Nov '24) deck and "Union / Flyte Colors: Hex and PMS codes"
sheet, supplied by the owner on 2026-11-15. Dated but current. Spacing and width are this workspace's
choice, not from the brand source. Default to Union branding; use the Flyte variant only for Flyte
(open-source project) material.

## Tokens

**Voice [VOICE]:** plain, factual, short sentences. No hype words. Numbers carry their caveat.
**Audience [AUDIENCE]:** ML and AI platform practitioners and their engineering leaders (Union work). A project that declares `[audience]` in its product.toml is judged for those users instead.
**Naming [NAMES]:** "Union" (the company and platform), "Flyte" (the open-source project), "the agent factory".

**Palette [PRIMARY] [SURFACE] [TEXT]** (Union, from the sheet's grey and yellow ranges):

| Token | Dark theme (default) | Light theme |
|---|---|---|
| Background | `#2B2E2F` (grey 20) | `#FCFDFD` (grey 99) |
| Deep background / text on light | `#1D1E1F` (grey 10) | n/a |
| Surface, hovers, accent boxes | `#434648` (grey 30) | `#F5F6F8` (grey 95), `#E6E9EA` (grey 90) |
| Lines, dividers | `#66696A` (grey 40) | `#D4D8DA` (grey 70) |
| Text | `#FFFFFF` (13.7:1 on grey 20) | `#1D1E1F` (16.4:1 on grey 99) |
| Muted text | `#9B9EA0` (grey 50, 5.1:1) or `#B1B5B7` (grey 60, 6.6:1) | `#66696A` (grey 40, 5.4:1) |
| CTA / accent | `#FCB51D` Union yellow (yellow 60; 7.7:1 on grey 20) | `#FCB51D` fills only; yellow text on light uses `#7E5700` (yellow 30, 6.3:1) |
| Text on a yellow fill | `#1D1E1F` (9.4:1 on `#FCB51D`) | same |

Yellow range for charts and states: `#5F4100` (20), `#7E5700` (30), `#9D6E00` (40), `#E19F00` (50),
`#FCB51D` (60), `#FDC979` (70), `#FFDEAB` (80), `#FFECCE` (90), `#FFF6E9` (95).

Flyte variant (Flyte material only): same greys; accent `#6F43C0` (purple 50) for purple on light (6.4:1 on
grey 99), `#A97FF6` (purple 70) for purple text on dark (4.6:1 on grey 20). Purple range: `#1D0D39`,
`#2E1757`, `#41227A`, `#5A32A4`, `#6F43C0`, `#8C61DD`, `#A97FF6`, `#C7A8FF`, `#DBC7FF`, `#F4EEFF`.

**Typography [FONTS] [SCALE]:** Inter (OFL) for everything, fallback `system-ui, "Helvetica Neue", Arial,
sans-serif`. Slides at 1920x1080: titles 88-110 px bold, labels 34-44 px, body 32-40 px, captions 50 px
semibold. Documents: 16 px body, 1.25 type scale.

**Logo [LOGO] [CLEAR_SPACE]:** official files in `brand/` at the repository root (kept outside `standards/` so sessions don't load them as text). SVG first; PNG with transparency
for tools that can't render SVG. Never draw, redraw, recolour, stretch or approximate a mark.

| Use | Dark background (default) | Light background |
|---|---|---|
| Union wordmark (default logo) | `union/wordmark-yellow` or `union/wordmark-white` | `union/wordmark-black` |
| Union symbol (small spaces, avatars) | `union/symbol-yellow` or `union/symbol-white` | `union/symbol-black` |
| "Union, creators of Flyte" lockup | `union-flyte-lockup/color-on-dark` or `mono-white` | `union-flyte-lockup/color-on-light` or `mono-black` |
| Flyte material only | `flyte/wordmark_ondark`, `flyte/wordmark_byUnion_Lg_ondark` | `flyte/wordmark_onLight`, `flyte/wordmark_byUnion_Lg_onLight` |

Clear space: at least the width of the symbol on every side (see `flyte/guideline-clear-space.png`; the same
rule applies to the Union marks). One logo per view; the yellow wordmark is the default on Union's dark grey.
Flyte variations by background are in `flyte/guideline-variations.png`.

**Spacing [SPACING] / width [WIDTH]:** 8 px base scale (8, 16, 24, 32, 48, 64, 96). Text measure at most
72 characters; slides keep a 96 px safe margin and a clear bottom band for captions.

**Examples [EXAMPLES]:**
- Do: dark grey background, white Inter text, one yellow accent per view, big type, real screenshots.
- Do not: gradients, emoji, stock imagery, yellow text on light backgrounds, mock UI passed off as real,
  the old bluer near-black (`#0E0F12`) backgrounds.

## BR-001 MUST
Use the voice, audience and naming above before publication.

Why: A template is not a brand decision.

Check: Search the deliverable for off-brand names and unfilled bracket placeholders.

Enforced: judgement

## BR-002 SHOULD
Use the chosen voice consistently; do use plain verbs, do not use unexplained jargon.

Why: Readers need a consistent voice.

Check: Compare headings and representative paragraphs with the chosen voice examples.

Enforced: judgement

## BR-003 MUST
Use the palette tokens above and meet WCAG AA contrast.

Why: Colour must remain readable and recognisably Union.

Check: Measure text contrast: at least 4.5:1 normally and 3:1 for large text; colours come from the table.

Enforced: judgement

## BR-004 SHOULD
Set typography as above, with fallback fonts.

Why: Type hierarchy guides attention.

Check: Inspect computed fonts and heading sizes against the tokens.

Enforced: judgement

## BR-005 MUST
Use only the official logo files in `brand/` with the clear space above; never draw or approximate a mark.

Why: Distorted marks confuse identity.

Check: Compare logo source, aspect ratio and surrounding space with the official file.

Enforced: judgement

## BR-006 SHOULD
Use the spacing scale and layout width above.

Why: Consistent rhythm improves scanning.

Check: Inspect representative page spacing against the tokens.

Enforced: judgement

## BR-007 MUST
Use purposeful original imagery; do not use stock imagery.

Why: Visuals must explain this specific work.

Check: Inspect each image source and state its purpose.

Enforced: judgement

## BR-008 MUST
Label chart axes, units and sources and avoid misleading scales.

Why: Charts must support accurate comparisons.

Check: Inspect every chart for labels, provenance and an appropriate baseline.

Enforced: judgement

## BR-009 MUST
Support keyboard access, visible focus and meaningful text alternatives.

Why: Visual surfaces must work with assistive technology.

Check: Navigate all controls by keyboard and inspect image alternatives.

Enforced: judgement

## BR-010 SHOULD
Follow the do and do-not examples above.

Why: Examples resolve ambiguous choices.

Check: Confirm the deliverable follows the do list and avoids the do-not list.

Enforced: judgement
