"""Check that the user-facing text reads at or near the audience's reading grade.

The measure is the Flesch-Kincaid grade level, 0.39 * words/sentences + 11.8 * syllables/words - 15.59, over
the whole corpus: the text of the declared pages as a browser renders them (so text a script inserts counts
and text CSS hides does not; served and http(s) pages too, started with [surfaces] serve when set), and the
files in [surfaces] text. The check fails when the corpus grade exceeds audience.reading_grade by more than
TOLERANCE (1.0 grade).

Pages are rendered with the render check's browser (ux_render); the HTML source is parsed only to name the
file:line a rendered block came from. Without node, Playwright or Chromium the check exits 127, never a pass
and never NOT APPLICABLE, and a page that fails to load or render fails the check, so a corpus that is only
partly measured never passes. A product with only [surfaces] text needs no browser.

Known limits, so nobody over-trusts a pass or a fail:
- English only. Other languages get meaningless numbers.
- It measures sentence length and word length, not clarity. Short words in a confusing order score well;
  a precise technical term scores badly. Treat it as a smoke alarm, not a judge.
- Short texts are noisy: one long sentence moves the grade by whole levels, so when the whole corpus has
  fewer than MIN_WORDS words nothing is measured and the check reports NOT APPLICABLE (the reviewer reads
  the text instead), and the grades listed per passage are for finding what to rewrite, not for failing.
  Short passages are graded in windows of at least PASSAGE_MIN_WORDS words with their neighbours in the
  same file, each named by the file:line where it starts.
- Syllables are counted by a vowel-group heuristic (see `count_syllables`); expect an error of a few percent.
- Interface text is fragmentary (labels, buttons). Each passage (a block of a page, a heading, list item or
  table row of Markdown) counts as at least one sentence, which pulls the grade down for label-heavy pages.
  In Markdown, link text is kept; fenced and indented code, front matter and HTML comments are left out.
- A file is read once however many patterns match it, and a declared page is not read again as a text file.

Exit 0: pass, or NOT APPLICABLE (no human-facing surface, or too little text). Exit 1: fail.
Exit 127: a declared page needs node, Playwright or Chromium and it is missing or broken.
Run from the project directory.
"""

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# The standards directory is not the project's: importing a sibling must not leave __pycache__ in it.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ux_common as common  # type: ignore[import-not-found]  # noqa: E402 -- this directory is put on sys.path above
import ux_render  # type: ignore[import-not-found]  # noqa: E402 -- this directory is put on sys.path above

TOLERANCE = 1.0
MIN_WORDS = 100
PASSAGE_MIN_WORDS = 30
HARDEST = 5
EXIT_NO_TOOL: int = ux_render.EXIT_NO_TOOL

# The part of a passage's label after its file: `:12` (a line), `:$.a.b` (a JSON path) or ` (rendered)`.
SOURCE_SUFFIX = re.compile(r"(:(\d+|\$.*)| \(rendered\))$")
WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
# A closing quote or bracket may follow the stop: `."`, `.”` and `.)` end a sentence too.
SENTENCE_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)")
# Elements that end a passage. Inline elements (a, span, em, ...) do not: their text joins the paragraph.
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "br", "button", "caption", "dd", "details", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "label", "li", "main", "nav", "ol", "option", "p", "pre", "section", "summary", "table",
    "td", "th", "tr", "ul",
}  # fmt: skip
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
NEVER_VISIBLE = {"head", "script", "style", "template", "noscript"}


def count_syllables(word: str) -> int:
    """Estimate the syllables in an English word: vowel groups less a silent final e, ed or es; min 1."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    count = len(re.findall(r"[aeiouy]+", w))
    if len(w) > 2:
        if w.endswith("e") and not re.search(r"[^aeiouy]le$", w):
            count -= 1
        elif w.endswith("ed") and not re.search(r"[td]ed$", w):
            count -= 1
        elif w.endswith("es") and not re.search(r"(?:[sxzcg]|ch|sh)es$", w):
            count -= 1
    return max(count, 1)


def count_words_and_syllables(text: str) -> tuple[int, int, int]:
    """Return (words, sentences, syllables) of `text`; non-empty text has at least one sentence."""
    words = WORD.findall(text)
    if not words:
        return 0, 0, 0
    sentences = max(len(SENTENCE_END.findall(text)), 1)
    return len(words), sentences, sum(count_syllables(w) for w in words)


def compute_grade_from_counts(words: int, sentences: int, syl: int) -> float:
    """Return the Flesch-Kincaid grade level of the counts."""
    return 0.39 * words / sentences + 11.8 * syl / words - 15.59


def compute_grade(text: str) -> float | None:
    """Return the Flesch-Kincaid grade of `text`, or None when it has no words."""
    c = count_words_and_syllables(text)
    return compute_grade_from_counts(*c) if c[0] else None


class _VisibleText(HTMLParser):
    """Collect visible text as (line, passage) pairs, skipping scripts, styles and hidden subtrees."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.passages: list[tuple[int, str]] = []
        self._stack: list[tuple[str, bool]] = []  # (tag, hides what is inside)
        self._buffer: list[str] = []
        self._line = 1

    def _hidden(self) -> bool:
        return any(hides for _, hides in self._stack)

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.passages.append((self._line, text))
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self._flush()
        if tag in VOID_TAGS:
            return
        names = dict(attrs)
        hides = (
            tag in NEVER_VISIBLE or "hidden" in names or (names.get("aria-hidden") or "").lower() == "true"
        )
        self._stack.append((tag, hides))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            self._flush()
        # Tolerate unclosed tags: pop back to the matching open tag, if there is one.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._hidden() or not data.strip():
            return
        if not self._buffer:
            self._line = self.getpos()[0]
        self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def extract_html_passages(path: Path) -> list[tuple[str, str]]:
    """Return (source, passage) for the visible text of an HTML file."""
    parser = _VisibleText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return [(f"{path}:{line}", text) for line, text in parser.passages]


def extract_json_passages(path: Path) -> list[tuple[str, str]]:
    """Return (source, string) for every string value in a JSON file, recursively."""
    found: list[tuple[str, str]] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, str):
            found.append((f"{path}:{where}", node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")

    walk(json.loads(path.read_text(encoding="utf-8")), "$")
    return found


def extract_prose_passages(path: Path) -> list[tuple[str, str]]:
    """Return (source, paragraph) for a plain text file: paragraphs end at blank lines."""
    paragraph: list[str] = []
    start = 0
    found: list[tuple[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            if paragraph:
                found.append((f"{path}:{start}", " ".join(" ".join(paragraph).split())))
            paragraph = []
            continue
        if not paragraph:
            start = number
        paragraph.append(line)
    if paragraph:
        found.append((f"{path}:{start}", " ".join(" ".join(paragraph).split())))
    return found


FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
# A heading, list item or table row is a passage of its own: none of them ends with a full stop, and joined to
# its neighbours sixteen short bullets would read as one sixty-word sentence.
OWN_PASSAGE = re.compile(r"^\s{0,3}(?:#{1,6}\s|\|)|^\s*(?:[-*+]|\d{1,9}[.)])\s+")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
SETEXT_RULE = re.compile(r"^\s{0,3}(?:=+|-+)\s*$")
THEMATIC_BREAK = re.compile(r"^\s{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
LINK_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s")
COMMENT_OPEN = re.compile(r"^\s{0,3}<!--")
# Front matter (a Jekyll, Hugo and Pandoc convention, not CommonMark): its opening line, and what closes it.
FRONT_MATTER = {"---": ("---", "..."), "+++": ("+++",)}


def strip_markdown(line: str) -> str:
    """Return the words a reader sees in a line of Markdown: link text kept, code, images and tags dropped."""
    line = re.sub(r"(`+).*?\1", " ", line)  # inline code
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]", " ", line)  # images
    line = re.sub(r"\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])", r"\1", line)  # links keep their text
    line = re.sub(r"<[^>]+>", " ", line)  # inline HTML and autolinks
    line = re.sub(r"^\s{0,3}(?:>\s?)+", "", line)  # block quote markers
    line = OWN_PASSAGE.sub("", line)  # the heading, list or table marker itself
    return re.sub(r"[|*_#]+", " ", line)


def count_front_matter_lines(lines: list[str]) -> int:
    """Return how many lines at the top of a Markdown file are front matter: 0 when there is none."""
    closers = FRONT_MATTER.get(lines[0].rstrip()) if lines else None
    if closers:
        for number, line in enumerate(lines[1:], 2):
            if line.rstrip() in closers:
                return number
    return 0


def extract_markdown_passages(path: Path) -> list[tuple[str, str]]:
    """Return (source, passage) for a Markdown file: paragraphs, and each heading, list item and table row.

    Each line is classified as a CommonMark block. What a reader never sees as prose is left out: front
    matter, fenced code (§4.5), indented code (§4.4, four columns past the current list item's content, a tab
    counting as four), HTML comments (§4.6), link reference definitions, table rules and thematic breaks.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    found: list[tuple[str, str]] = []
    passage: list[str] = []
    start = 0
    fence = ""
    in_comment = False
    in_code = False
    item = 0  # the column a list item's content starts at; 0 outside a list

    def flush() -> None:
        text = " ".join(" ".join(passage).split())
        if text:
            found.append((f"{path}:{start}", text))
        passage.clear()

    def after_comment(line: str, number: int) -> bool:
        """Keep the text after a comment's close (the browser shows it); return whether it is still open."""
        if "-->" not in line:
            return True
        text = " ".join(strip_markdown(line.split("-->", 1)[1]).split())
        if text:
            found.append((f"{path}:{number}", text))
        return False

    skip = count_front_matter_lines(lines)
    for number, line in enumerate(lines, 1):
        if number <= skip:
            continue
        opened = FENCE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = ""
            continue
        if in_comment:
            in_comment = after_comment(line, number)
            continue
        blank = not line.strip()
        column = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())
        if in_code:
            if blank or column >= item + 4:
                continue
            in_code = False
        if blank:
            flush()
            continue
        if item and not passage and column < item and not LIST_ITEM.match(line):
            item = 0  # a line after a blank, left of the item's content, ends the list
        if column >= item + 4 and not passage:
            in_code = True  # an indented code block cannot interrupt a paragraph, so only when none is open
            continue
        if opened:
            flush()
            fence = opened.group(1)
            continue
        if COMMENT_OPEN.match(line):
            flush()
            in_comment = after_comment(line.split("<!--", 1)[1], number)
            continue
        if TABLE_RULE.match(line) and "-" in line or LINK_DEFINITION.match(line):
            flush()
            continue
        if SETEXT_RULE.match(line) and passage:
            flush()  # the line above was a heading
            continue
        if THEMATIC_BREAK.match(line):
            flush()
            continue
        listed = LIST_ITEM.match(line)
        if listed:
            item = len(listed.group(0).expandtabs(4))
        if OWN_PASSAGE.match(line):
            flush()
        if not passage:
            start = number
        passage.append(strip_markdown(line))
        if re.match(r"^\s{0,3}(?:#{1,6}\s|\|)", line):
            flush()  # a heading or table row never continues on the next line
    flush()
    return found


def _find_source_line(static: list[tuple[str, str]], block: str) -> str | None:
    """Return the file:line of the source passage a rendered block came from, when it can be told."""
    needle = block[:40]
    for source, text in static:
        if needle in text or (len(text) >= 20 and text[:40] in block):
            return source
    return None


def extract_page_passages(
    root: Path, config: dict[str, Any], pages: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (passages, problems) for the declared pages, as a browser renders them.

    The HTML source is parsed only to name the file:line a rendered block came from, never as the text itself.
    A page that cannot be rendered is a problem, so a part of the corpus never stands for the whole.
    Raises ux_render.ToolUnavailable when there is no browser to render with.
    """
    passages: list[tuple[str, str]] = []
    problems: list[str] = []
    local = []
    for page in pages:
        if re.match(r"https?://", page):
            continue
        if not (root / page).is_file():
            problems.append(f"surfaces.pages: {page} does not exist")
        else:
            local.append(page)
    if problems or not pages:
        return passages, problems
    surfaces = common.get_surfaces_table(config)
    viewports = surfaces.get("viewports") or common.DEFAULT_VIEWPORTS
    largest = max(viewports, key=lambda v: tuple(int(n) for n in v.split("x")))
    rendered, why = ux_render.get_rendered_text(root, surfaces, pages, largest)
    if why:
        return passages, [f"surfaces.pages: not rendered: {why}"]
    for page in pages:
        blocks = rendered.get(page)
        if isinstance(blocks, list):
            static = extract_html_passages(root / page) if page in local else []
            passages += [(_find_source_line(static, b) or f"{page} (rendered)", b) for b in blocks]
        else:
            problems.append(f"surfaces.pages: {page} could not be rendered: {blocks or 'no result'}")
    return passages, problems


def collect_passages(root: Path, config: dict[str, Any]) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (passages, problems) for the declared pages and text files."""
    pages = common.get_declared_pages(config)
    passages, problems = extract_page_passages(root, config, pages)
    # A file is read once, however many patterns match it and whether or not it is also a declared page.
    seen = {(root / page).resolve() for page in pages if not re.match(r"https?://", page)}
    for pattern in common.get_declared(config, "text"):
        matches, match_problems = common.find_text_matches(root, pattern)
        problems += match_problems
        for path in matches:
            if (root / path).resolve() in seen:
                continue
            seen.add((root / path).resolve())
            try:
                if path.suffix.lower() == ".json":
                    passages += extract_json_passages(path)
                elif path.suffix.lower() in {".html", ".htm"}:
                    passages += extract_html_passages(path)
                elif path.suffix.lower() in {".md", ".markdown"}:
                    passages += extract_markdown_passages(path)
                else:
                    passages += extract_prose_passages(path)
            except (ValueError, OSError) as error:  # JSON errors are ValueErrors
                problems.append(f"{path}: cannot read ({error})")
    return passages, problems


def main() -> int:
    """Run the check in the current directory."""
    root = Path.cwd()
    config, parse_error = common.load_config(root)
    common.exit_if_malformed(config, root)
    if not common.has_surface(root, config):
        common.exit_not_applicable("no human-facing surface")
    audience = (config or {}).get("audience")
    problems = [parse_error] if parse_error else []
    if isinstance(audience, dict):
        problems += common.find_reading_grade_problems(audience)
    elif not parse_error:
        problems.append("audience.reading_grade: missing (no [audience] in product.toml)")
    if problems or not isinstance(audience, dict) or config is None:
        print("FAIL: no usable reading grade to measure against")
        for problem in problems:
            print(f"  - {problem}")
        print("Fix: set [audience] reading_grade in product.toml; see the audience check for an example.")
        return 1
    target = audience["reading_grade"]
    try:
        passages, read_problems = collect_passages(root, config)
    except ux_render.ToolUnavailable as error:
        print(f"{error}; cannot render pages")
        return EXIT_NO_TOOL
    if read_problems:
        print("FAIL: declared text could not be read")
        for problem in read_problems:
            print(f"  - {problem}")
        return 1
    return measure_reading_grade(passages, target)


def make_passage_windows(passages: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Group runs of short neighbouring passages of one file into windows of at least PASSAGE_MIN_WORDS words.

    A passage of PASSAGE_MIN_WORDS words or more stands alone. Each window is labelled with the file:line
    where it starts; a run's short tail joins the window before it, and a run too short for one window is a
    window of its own, so every file can be named.
    """
    runs: list[list[tuple[str, str]]] = []
    run_file = None
    for src, text in passages:
        file = SOURCE_SUFFIX.sub("", src)
        if len(WORD.findall(text)) >= PASSAGE_MIN_WORDS:
            runs.append([(src, text)])
            run_file = None
        elif file == run_file:
            runs[-1].append((src, text))
        else:
            runs.append([(src, text)])
            run_file = file
    windows: list[tuple[str, list[str]]] = []
    for run in runs:
        start, size = len(windows), PASSAGE_MIN_WORDS
        for src, text in run:
            if size >= PASSAGE_MIN_WORDS:
                windows.append((src, []))
                size = 0
            windows[-1][1].append(text)
            size += len(WORD.findall(text))
        if size < PASSAGE_MIN_WORDS and len(windows) - start > 1:
            windows[-2][1].extend(windows.pop()[1])
    return windows


def measure_reading_grade(passages: list[tuple[str, str]], target: float) -> int:
    """Print the grade of `passages` against `target` and the hardest of them; return the exit code."""
    total = (
        [sum(c) for c in zip(*(count_words_and_syllables(text) for _, text in passages), strict=True)]
        if passages
        else [0, 0, 0]
    )
    if total[0] < MIN_WORDS:
        print(f"NOT APPLICABLE: only {total[0]} words of text, too few to measure; the reviewer judges it")
        print(
            f"too little text to measure ({total[0]} words over every page and text file; {MIN_WORDS} needed)"
        )
        return 0
    corpus = compute_grade_from_counts(*total)
    limit = target + TOLERANCE
    verdict = "FAIL" if corpus > limit else "PASS"
    print(
        f"{verdict}: reading grade {corpus:.1f} over {total[0]} words; target {target},"
        f" tolerance {TOLERANCE}, limit {limit:.1f} (Flesch-Kincaid, English only)"
    )
    if verdict == "PASS":
        return 0
    # Short passages (labels, list items) are graded in windows with their neighbours, so a failure always
    # names a file:line to rewrite.
    windows = [
        (
            compute_grade_from_counts(
                *[sum(c) for c in zip(*(count_words_and_syllables(t) for t in texts), strict=True)]
            ),
            src,
            " ".join(texts),
        )
        for src, texts in make_passage_windows(passages)
    ]
    windows.sort(key=lambda p: p[0], reverse=True)
    print(f"Hardest passages (windows of about {PASSAGE_MIN_WORDS} words; file:line is where each starts):")
    for value, src, text in windows[:HARDEST]:
        print(f"  {src}  grade {value:.1f}  {text[:80]}{'...' if len(text) > 80 else ''}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
