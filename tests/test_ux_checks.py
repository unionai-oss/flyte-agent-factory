"""Tests of the UX check scripts that need no browser: the audience check and the reading-level check."""

import importlib.util
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

CHECKS = (
    Path(__file__).resolve().parents[1]
    / "src/flyte_agent_factory/_assets/workspace_template/standards/checks"
)

AUDIENCE = """\
[audience]
users = "Children learning to read"
age_band = "6-8"
expertise = "novice"
reading_grade = {grade}
main_task = "Create a world and walk into its first room"
"""

EASY = "The cat sat on the mat. The dog ran to the log. We can go and see the sun. " * 8
HARD = (
    "Notwithstanding any provision to the contrary herein, the indemnifying party shall unconditionally "
    "compensate the aggrieved organisation for consequential liabilities arising from unauthorised "
    "dissemination of confidential information. "
) * 4

#: First-reader dialogue: each full stop sits inside the closing quote.
DIALOGUE = 'Sam said, "I can see the dog." Pam said, "The dog is big."'


def run(script: str, project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKS / script)], cwd=project, capture_output=True, text=True, timeout=60
    )


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, CHECKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CHECKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(CHECKS))
    return module


def project(tmp_path: Path, toml: str | None = None, **files: str) -> Path:
    if toml is not None:
        (tmp_path / "product.toml").write_text(toml)
    for name, text in files.items():
        # Keyword names stand for paths: `docs__guide_md` is docs/guide.md.
        path = tmp_path / re.sub(r"_(html|json|md)$", r".\1", name.replace("__", "/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def page(text: str) -> str:
    return f"<!doctype html><html lang=en><head><title>t</title></head><body>{text}</body></html>"


# --- audience ---


def test_audience_is_not_applicable_without_a_surface_or_product_toml(tmp_path):
    (tmp_path / "tool.py").write_text("print(1)\n")
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 0
    assert done.stdout.splitlines()[0].startswith("NOT APPLICABLE:")


def test_audience_fails_for_a_ui_without_product_toml_and_shows_an_example(tmp_path):
    project(tmp_path, index_html=page("<p>hi</p>"))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "product.toml: missing" in done.stdout
    assert "reading_grade = 2" in done.stdout  # the example block


def test_audience_passes_with_every_field_and_pages(tmp_path):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page("<p>hi</p>"))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS")


def test_audience_passes_without_a_ui_and_without_pages(tmp_path):
    project(tmp_path, AUDIENCE.format(grade=2))
    assert run("ux_audience.py", tmp_path).returncode == 0


def test_audience_names_each_missing_or_invalid_field(tmp_path):
    toml = '[audience]\nusers = ""\nexpertise = "guru"\nreading_grade = 19\n'
    project(tmp_path, toml)
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    for field in ("users", "age_band", "expertise", "reading_grade", "main_task"):
        assert f"audience.{field}" in done.stdout


@pytest.mark.parametrize(
    ("toml", "name", "text"),
    [
        (None, "index.html", page("<p>hi</p>")),
        (None, "pyproject.toml", '[project]\nname = "t"\n[project.scripts]\nmytool = "t:main"\n'),
        ('[surfaces]\npages = ["index.html"]\n', "index.html", page("<p>hi</p>")),
        ('audience = "children"\n[surfaces]\npages = ["index.html"]\n', "index.html", page("<p>hi</p>")),
    ],
    ids=["ui-without-product-toml", "cli-without-product-toml", "no-audience-table", "audience-not-a-table"],
)
def test_audience_names_every_field_when_the_table_or_the_file_is_missing(tmp_path, toml, name, text):
    (tmp_path / name).write_text(text)
    if toml is not None:
        (tmp_path / "product.toml").write_text(toml)
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1, done.stdout
    for field in ("users", "age_band", "expertise", "reading_grade", "main_task"):
        assert f"  - audience.{field}: missing" in done.stdout, (field, done.stdout)


@pytest.mark.parametrize("grade", ["true", '"2"', "2.5", "-1"])
def test_audience_rejects_reading_grades_that_are_not_integers_in_range(tmp_path, grade):
    project(tmp_path, AUDIENCE.format(grade=grade))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "audience.reading_grade" in done.stdout


def audience_toml(age_band: str, under_13: str | None = None) -> str:
    extra = "" if under_13 is None else f"under_13 = {under_13}\n"
    return AUDIENCE.format(grade=2).replace('age_band = "6-8"', f"age_band = {age_band}") + extra


@pytest.mark.parametrize(
    ("age_band", "under_13", "older"),
    [
        ('"6-8"', True, False),
        ('"10-14"', True, False),
        ('"12-14"', True, False),
        ('"13-17"', False, False),
        ('"13+"', False, False),
        ('"18-64"', False, False),
        ('"65+"', False, True),
        ('"65-80"', False, True),
        ('"60-80"', False, False),
        ('"adult"', False, False),
        ('"Adults"', False, False),
        ('"8 - 15"', True, False),
        ('"6\u20138"', True, False),
        ('"8-12, 65+"', True, True),
        ('"adults and 10-12"', True, False),
        ('"5+"', True, False),
    ],
)
def test_age_bands_that_can_be_read_say_whether_children_under_13_are_included(age_band, under_13, older):
    common = load("_ux_common")
    assert common.parse_age_band(age_band.strip('"')) == (under_13, older)


@pytest.mark.parametrize(
    "age_band", ["8-6", "kids", "teenagers and parents", "13", "0-200", "adult-ish", "-5"]
)
def test_age_bands_that_cannot_be_read_have_no_answer(age_band):
    assert load("_ux_common").parse_age_band(age_band) is None


@pytest.mark.parametrize(
    ("age_band", "under_13"),
    [('"6-8"', None), ('"12-14"', None), ('"13-17"', None), ('"13+"', None), ('"adult"', None),
     ('"65+"', None), ('"teenagers and parents"', "true"), ('"mixed"', "false"), ('"6-8"', "true"),
     ('"adult"', "false")],
)  # fmt: skip
def test_audience_passes_when_the_age_is_clear(tmp_path, age_band, under_13):
    project(tmp_path, audience_toml(age_band, under_13))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize("age_band", ['"teenagers and parents"', '"families"', '"everyone"'])
def test_audience_fails_for_a_vague_age_band_without_under_13(tmp_path, age_band):
    project(tmp_path, audience_toml(age_band))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "audience.under_13" in done.stdout and "true or false" in done.stdout


@pytest.mark.parametrize(
    ("age_band", "under_13"),
    [('"6-8"', "false"), ('"12-14"', "false"), ('"13-17"', "true"), ('"13+"', "true"), ('"adult"', "true")],
)
def test_audience_fails_when_under_13_contradicts_the_age_band(tmp_path, age_band, under_13):
    project(tmp_path, audience_toml(age_band, under_13))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "audience.under_13" in done.stdout and "audience.age_band" in done.stdout
    assert age_band.strip('"') in done.stdout


@pytest.mark.parametrize("under_13", ['"yes"', "1", '"true"'])
def test_audience_rejects_an_under_13_that_is_not_a_boolean(tmp_path, under_13):
    project(tmp_path, audience_toml('"6-8"', under_13))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "audience.under_13: must be true or false" in done.stdout


def test_audience_fails_on_unparsable_toml(tmp_path):
    project(tmp_path, "[audience\n")
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "does not parse" in done.stdout


def test_audience_asks_for_pages_when_ui_files_exist(tmp_path):
    project(tmp_path, AUDIENCE.format(grade=2), index_html=page("<p>hi</p>"))
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "declare the pages to render" in done.stdout


def test_audience_ignores_ui_files_in_vendored_directories(tmp_path):
    project(tmp_path, None, node_modules__pkg__x_html=page("<p>hi</p>"))
    assert run("ux_audience.py", tmp_path).stdout.startswith("NOT APPLICABLE:")


def test_audience_counts_a_built_dist_as_a_surface(tmp_path):
    project(tmp_path, None, dist__index_html=page("<p>hi</p>"))
    assert run("ux_audience.py", tmp_path).returncode == 1


# --- reading level: the arithmetic ---


def test_syllables_of_known_words():
    count_syllables = load("ux_reading_level").count_syllables
    expected = {"cat": 1, "make": 1, "table": 2, "little": 2, "played": 1, "planted": 2, "beautiful": 3}
    assert {w: count_syllables(w) for w in expected} == expected
    assert count_syllables("the") == 1  # a silent e never takes a word to zero


def test_grade_of_a_first_reader_sentence_is_below_one():
    assert load("ux_reading_level").compute_grade("The cat sat on the mat.") < 1


def test_sentences_end_before_a_closing_quote_or_bracket():
    module = load("ux_reading_level")
    assert module.count_words_and_syllables(DIALOGUE)[1] == 2
    assert module.count_words_and_syllables("(See the dog.) Go home.")[1] == 2
    assert module.count_words_and_syllables("He said “Go.” Then “Stop.”")[1] == 2
    assert module.compute_grade(" ".join([DIALOGUE] * 10)) < 2


def test_grade_of_dense_legal_prose_is_above_twelve():
    assert load("ux_reading_level").compute_grade(HARD) > 12


def test_grade_matches_the_formula_for_known_counts():
    module = load("ux_reading_level")
    # 100 words in 5 sentences with 150 syllables: 0.39*20 + 11.8*1.5 - 15.59
    assert module.compute_grade_from_counts(100, 5, 150) == pytest.approx(9.91, abs=0.01)
    assert module.compute_grade("") is None


# --- reading level: the check ---


def test_reading_level_is_not_applicable_without_a_surface(tmp_path):
    done = run("ux_reading_level.py", project(tmp_path, AUDIENCE.format(grade=2)))
    assert done.stdout.startswith("NOT APPLICABLE:") and done.returncode == 0


def test_reading_level_needs_a_valid_reading_grade(tmp_path):
    project(tmp_path, None, index_html=page("<p>hi</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1
    assert "reading_grade" in done.stdout


def test_reading_level_passes_easy_text(tmp_path):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page(f"<p>{EASY}</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS: reading grade")


@pytest.mark.parametrize("story", [DIALOGUE, DIALOGUE.replace('."', '".')])
def test_reading_level_passes_easy_dialogue_in_a_text_file(tmp_path, story):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["story.txt"]\n'
    project(tmp_path, toml, **{"story.txt": " ".join([story] * 10)})
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS: reading grade")


def test_reading_level_fails_hard_text_and_lists_the_hardest_passages(tmp_path):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
    body = f"<p>{EASY}</p>\n<p>{HARD}</p>"
    project(tmp_path, toml, index_html=page(body))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1
    assert "tolerance 1.0" in done.stdout
    # The hard paragraph (line 2) is listed first, as file:line with its grade.
    assert "index.html:2  grade " in done.stdout
    assert done.stdout.index("index.html:2") < done.stdout.index("index.html:1")
    assert "Notwithstanding" in done.stdout


def test_reading_level_names_the_file_and_line_when_every_passage_is_short(tmp_path):
    # Label-heavy text: no passage reaches 30 words, yet the failure still points at what to rewrite.
    toml = AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["*.md"]\n'
    hard = "- Photosynthesis necessitates chlorophyll-mediated electromagnetic absorption.\n" * 12
    easy = "- The cat sat on the mat.\n" * 10
    project(tmp_path, toml, hard_md=hard, easy_md=easy)
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1, done.stdout
    assert "hard.md:1  grade " in done.stdout and "Photosynthesis" in done.stdout
    assert "easy.md:1  grade " in done.stdout
    assert done.stdout.index("hard.md:1") < done.stdout.index("easy.md:1")


def words(n: int) -> str:
    return " ".join(["word"] * n)


def test_window_passages_joins_short_neighbours_from_the_same_file():
    make_passage_windows = load("ux_reading_level").make_passage_windows
    passages = [
        ("a.md:1", words(10)),
        ("a.md:3", words(25)),
        ("a.md:5", words(40)),
        ("b.md:1", words(5)),
        ("c.json:$.x", words(20)),
        ("c.json:$.y", words(15)),
        ("c.json:$.z", words(4)),
        ("index.html (rendered)", words(20)),
        ("index.html:9", words(20)),
    ]
    assert make_passage_windows(passages) == [
        ("a.md:1", [words(10), words(25)]),
        # A passage long enough on its own is graded on its own.
        ("a.md:5", [words(40)]),
        # A file too short for a whole window is still listed, under its first line.
        ("b.md:1", [words(5)]),
        # A short tail joins the window before it rather than standing alone.
        ("c.json:$.x", [words(20), words(15), words(4)]),
        ("index.html (rendered)", [words(20), words(20)]),
    ]


def test_reading_level_allows_the_tolerance(tmp_path):
    # Grade 2 text measured against a target of 1 is inside the one-grade tolerance.
    toml = AUDIENCE.format(grade=0) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page(f"<p>{EASY}</p>"))
    assert run("ux_reading_level.py", tmp_path).returncode == 0


def test_reading_level_measures_nothing_below_a_hundred_words(tmp_path):
    toml = AUDIENCE.format(grade=0) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page(f"<p>{HARD[:200]}</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0
    assert "too little text to measure (" in done.stdout


def test_reading_level_skips_scripts_styles_and_hidden_text(tmp_path):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
    body = (
        f"<p>{EASY}</p><script>var s = '{HARD}';</script><style>/* {HARD} */</style>"
        f"<div hidden><p>{HARD}</p></div><div aria-hidden='true'>{HARD}</div>"
    )
    project(tmp_path, toml, index_html=page(body))
    assert run("ux_reading_level.py", tmp_path).returncode == 0


def test_reading_level_reads_declared_text_files(tmp_path):
    toml = (
        AUDIENCE.format(grade=2)
        + '[surfaces]\npages = ["index.html"]\ntext = ["src/strings/*.json", "docs/*.md"]\n'
    )
    strings = json.dumps({"a": {"intro": HARD}, "b": [EASY]})
    markdown = f"# Title\n\n```\n{HARD}\n```\n\n{EASY}\n"
    project(
        tmp_path, toml, index_html=page("<p>hi</p>"), src__strings__en_json=strings, docs__guide_md=markdown
    )
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1
    assert "src/strings/en.json:$.a.intro" in done.stdout
    # The code block (line 4) is not prose; the easy paragraph after it (line 7) is listed.
    assert "guide.md:4" not in done.stdout and "guide.md:7  grade -" in done.stdout


def test_reading_level_fails_on_a_text_glob_that_matches_nothing(tmp_path):
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\ntext = ["nope/*.json"]\n'
    project(tmp_path, toml, index_html=page("<p>hi</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1
    assert "nope/*.json matches no file" in done.stdout


# --- walkthrough ---

WALK_TOML = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
WALK_TEXT = (
    "# Walkthrough\n\nThe task: create a world  and walk into\nits first room.\n\n"
    "![start](index@320x640.png)\n\n<img src='index@1280x800.png' alt='start, wide'>\n"
)


def png(width: int, height: int) -> bytes:
    """Return a grey 8-bit PNG of the given size: signature, IHDR, one IDAT, IEND."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    rows = (b"\x00" + b"\x80" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def write_screenshot(path: Path) -> None:
    """Write a PNG as large as the `@WxH` in the file's name."""
    width, height = re.search(r"@(\d+)x(\d+)", path.name).groups()  # type: ignore[union-attr]
    path.write_bytes(png(int(width), int(height)))


def walk_project(
    tmp_path: Path,
    walkthrough: str | None = WALK_TEXT,
    images=("index@320x640.png", "index@1280x800.png"),
    toml: str = WALK_TOML,
) -> Path:
    project(tmp_path, toml, index_html=page("<p>hi</p>"))
    evidence = tmp_path / "ux-evidence"
    evidence.mkdir()
    for name in images:
        write_screenshot(evidence / name)
    if walkthrough is not None:
        (evidence / "WALKTHROUGH.md").write_text(walkthrough)
    return tmp_path


def test_walkthrough_passes_with_the_task_and_images_at_both_ends(tmp_path):
    done = run("ux_walkthrough.py", walk_project(tmp_path))
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS")


def test_walkthrough_is_not_applicable_without_a_surface(tmp_path):
    (tmp_path / "tool.py").write_text("print(1)\n")
    done = run("ux_walkthrough.py", tmp_path)
    assert done.returncode == 0
    assert done.stdout.splitlines()[0].startswith("NOT APPLICABLE:")


def test_walkthrough_fails_when_the_file_is_missing(tmp_path):
    done = run("ux_walkthrough.py", walk_project(tmp_path, walkthrough=None))
    assert done.returncode == 1
    assert "WALKTHROUGH.md: missing" in done.stdout


def test_walkthrough_fails_without_the_main_task(tmp_path):
    text = WALK_TEXT.replace("create a world  and walk into\nits first room", "do something")
    done = run("ux_walkthrough.py", walk_project(tmp_path, text))
    assert done.returncode == 1
    assert "main task" in done.stdout and "Create a world and walk into its first room" in done.stdout


def test_walkthrough_points_at_the_audience_check_when_main_task_is_unknown(tmp_path):
    done = run("ux_walkthrough.py", walk_project(tmp_path, toml='[surfaces]\npages = ["index.html"]\n'))
    assert done.returncode == 1
    assert "audience.main_task is missing" in done.stdout and "audience check" in done.stdout


def test_walkthrough_fails_for_a_linked_image_that_does_not_exist(tmp_path):
    done = run("ux_walkthrough.py", walk_project(tmp_path, images=("index@320x640.png",)))
    assert done.returncode == 1
    assert "index@1280x800.png is linked but does not exist" in done.stdout


def test_walkthrough_fails_with_fewer_than_two_images(tmp_path):
    text = WALK_TEXT.split("<img")[0]
    done = run("ux_walkthrough.py", walk_project(tmp_path, text))
    assert done.returncode == 1
    assert "1 linked, at least 2 needed" in done.stdout


def test_walkthrough_fails_when_only_one_viewport_is_shown(tmp_path):
    text = WALK_TEXT.replace("index@1280x800.png", "step2@320x640.png")
    done = run(
        "ux_walkthrough.py", walk_project(tmp_path, text, images=("index@320x640.png", "step2@320x640.png"))
    )
    assert done.returncode == 1
    assert "largest viewport 1280x800" in done.stdout
    assert "smallest" not in done.stdout


def test_walkthrough_uses_the_declared_viewports(tmp_path):
    toml = WALK_TOML + 'viewports = ["360x640", "1024x768"]\n'
    done = run("ux_walkthrough.py", walk_project(tmp_path, toml=toml))
    assert done.returncode == 1
    assert "smallest viewport 360x640" in done.stdout and "largest viewport 1024x768" in done.stdout


# --- reports, CLIs and text-only products ---

REPORT_TOML = AUDIENCE.format(grade=6) + '[surfaces]\ntext = ["report.md"]\n'
#: Dense prose, far above grade 20 by the formula.
DENSE = (
    "The organisational reconfiguration necessitates comprehensive reevaluation of interdepartmental "
    "responsibilities, notwithstanding considerable implementation uncertainties regarding infrastructural "
    "dependencies and contractual obligations. "
) * 6


def test_a_text_only_report_at_too_high_a_grade_fails_the_reading_level_naming_the_file(tmp_path):
    done = run("ux_reading_level.py", project(tmp_path, REPORT_TOML, report_md=DENSE))
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL: reading grade")
    assert "report.md:1  grade " in done.stdout


def test_a_text_only_report_needs_a_walkthrough(tmp_path):
    done = run("ux_walkthrough.py", project(tmp_path, REPORT_TOML, report_md=DENSE))
    assert done.returncode == 1
    assert "WALKTHROUGH.md: missing" in done.stdout


def test_a_cli_declared_in_product_toml_is_human_facing(tmp_path):
    toml = AUDIENCE.format(grade=6) + '[surfaces]\ncli = ["mytool", "mytool --help"]\n'
    done = run("ux_walkthrough.py", project(tmp_path, toml))
    assert done.returncode == 1
    assert "WALKTHROUGH.md: missing" in done.stdout
    assert run("ux_audience.py", tmp_path).returncode == 0


def test_an_audience_alone_makes_the_project_human_facing(tmp_path):
    done = run("ux_walkthrough.py", project(tmp_path, AUDIENCE.format(grade=6)))
    assert done.returncode == 1
    assert "WALKTHROUGH.md: missing" in done.stdout


CLI_WALK = (
    "# Walkthrough\n\nThe task: create a world and walk into its first room.\n\n"
    "```\n$ mytool new world\nCreated world 'world'.\n```\n"
)


def cli_project(tmp_path: Path, walkthrough: str) -> Path:
    toml = AUDIENCE.format(grade=6) + '[surfaces]\ncli = "mytool"\n'
    (tmp_path / "ux-evidence").mkdir()
    (tmp_path / "ux-evidence/WALKTHROUGH.md").write_text(walkthrough)
    return project(tmp_path, toml)


def test_a_cli_walkthrough_may_show_a_transcript_instead_of_screenshots(tmp_path):
    done = run("ux_walkthrough.py", cli_project(tmp_path, CLI_WALK))
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS")


def test_a_text_walkthrough_may_quote_the_passage(tmp_path):
    text = (
        "# Walkthrough\n\nThe task: create a world and walk into its first room.\n\n> First, open the map.\n"
    )
    assert run("ux_walkthrough.py", cli_project(tmp_path, text)).returncode == 0


def test_a_cli_walkthrough_with_no_evidence_fails(tmp_path):
    text = "# Walkthrough\n\nThe task: create a world and walk into its first room. It worked.\n"
    done = run("ux_walkthrough.py", cli_project(tmp_path, text))
    assert done.returncode == 1
    assert "evidence: no screenshot, terminal transcript" in done.stdout


@pytest.mark.parametrize(
    "fence", ["```\n```\n", "```\n\n  \n```\n", "```\n$ mytool new world\n", "> \n> ...\n"]
)
def test_a_cli_walkthrough_with_an_empty_transcript_fails(tmp_path, fence):
    text = "# Walkthrough\n\nThe task: create a world and walk into its first room.\n\n" + fence
    done = run("ux_walkthrough.py", cli_project(tmp_path, text))
    assert done.returncode == 1
    assert "evidence: the transcript is empty; show the command and its output" in done.stdout
    assert "PASS" not in done.stdout


def test_a_cli_walkthrough_still_needs_its_linked_images_to_exist(tmp_path):
    done = run("ux_walkthrough.py", cli_project(tmp_path, CLI_WALK + "\n![run](run.png)\n"))
    assert done.returncode == 1
    assert "run.png is linked but does not exist" in done.stdout


@pytest.mark.parametrize("kind", ["cli", "pages"])
@pytest.mark.parametrize("link", ["dir", "file"])
def test_a_walkthrough_reached_through_a_symlink_out_of_the_project_fails(tmp_path, link, kind):
    # A complete walkthrough that lives elsewhere is not evidence about this project.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if kind == "cli":
        cli_project(elsewhere, CLI_WALK)
    else:
        walk_project(elsewhere)
    root = tmp_path / "proj"
    root.mkdir()
    for name in ("product.toml", "index.html"):
        if (elsewhere / name).is_file():
            shutil.copy(elsewhere / name, root / name)
    if link == "dir":
        (root / "ux-evidence").symlink_to(elsewhere / "ux-evidence")
    else:
        shutil.copytree(elsewhere / "ux-evidence", root / "ux-evidence")
        (root / "ux-evidence/WALKTHROUGH.md").unlink()
        (root / "ux-evidence/WALKTHROUGH.md").symlink_to(elsewhere / "ux-evidence/WALKTHROUGH.md")
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "outside the project" in done.stdout
    assert "missing" not in done.stdout
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", '[project]\nname = "t"\n[project.scripts]\nmytool = "t:main"\n'),
        ("package.json", '{"name": "t", "bin": {"mytool": "cli.js"}}'),
        ("package.json", '{"name": "t", "bin": "cli.js"}'),
    ],
)
def test_audience_fails_for_a_cli_without_product_toml(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    done = run("ux_audience.py", tmp_path)
    assert done.returncode == 1
    assert "product.toml: missing" in done.stdout and "command-line entry point" in done.stdout
    assert "[surfaces] cli" in done.stdout


def test_a_pyproject_without_scripts_is_not_a_cli(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "t"\n')
    assert run("ux_audience.py", tmp_path).stdout.startswith("NOT APPLICABLE:")


# --- short text ---


def test_reading_level_under_the_floor_is_not_applicable_and_says_why(tmp_path):
    toml = AUDIENCE.format(grade=0) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page(f"<p>{HARD[:200]}</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0
    first = done.stdout.splitlines()[0]
    assert re.fullmatch(
        r"NOT APPLICABLE: only \d+ words of text, too few to measure; the reviewer judges it", first
    )


def test_reading_level_floor_counts_pages_and_text_together(tmp_path):
    # Each file alone is under 100 words; together they are over it, so the grade is measured.
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\ntext = ["notes.md"]\n'
    sentences = HARD.split(". ")
    half, other = ". ".join(sentences[:2]) + ".", ". ".join(sentences[2:4]) + "."
    assert all(len(load("ux_reading_level").WORD.findall(t)) < 100 for t in (half, other))
    project(tmp_path, toml, index_html=page(f"<p>{half}</p>"), notes_md=other)
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL: reading grade")


# --- malformed [surfaces] ---

MALFORMED = ['surfaces = "x"', '[surfaces]\ntext = "x"', '[surfaces]\npages = "x"', "[surfaces]\ncli = 3"]


@pytest.mark.parametrize(
    "script", ["ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py"]
)
@pytest.mark.parametrize("surfaces", MALFORMED)
def test_malformed_surfaces_fail_with_a_message_in_every_script(tmp_path, script, surfaces):
    # Top-level keys come before any table, or TOML puts them inside it.
    project(tmp_path, surfaces + "\n" + AUDIENCE.format(grade=2))
    done = run(script, tmp_path)
    assert done.returncode == 1, done.stdout
    assert "Traceback" not in done.stdout + done.stderr
    assert "FAIL" in done.stdout and "surfaces" in done.stdout


# --- malformed serve, ready and viewports; paths outside the project ---

BAD_VALUES = [
    ('serve = 5\nready = "http://127.0.0.1:9/"', "surfaces.serve"),
    ('serve = ["a"]\nready = "http://127.0.0.1:9/"', "surfaces.serve"),
    ('serve = "true"\nready = 7', "surfaces.ready"),
    ('serve = "true"\nready = "localhost:5998"', "surfaces.ready"),
    ('serve = "true"', "surfaces.ready"),
    ('viewports = ["0x0"]', "surfaces.viewports"),
    ('viewports = ["320x20000"]', "surfaces.viewports"),
    ('pages = ["/etc/hostname"]', "absolute path"),
    ('pages = ["../outside.html"]', "outside the project"),
    ('text = ["/etc/*.conf"]', "absolute path"),
    ('text = ["../*.md"]', "outside the project"),
]


@pytest.mark.parametrize(
    "script", ["ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py"]
)
@pytest.mark.parametrize(("surfaces", "named"), BAD_VALUES)
def test_bad_surface_values_and_escaping_paths_fail_in_every_script(tmp_path, script, surfaces, named):
    work = tmp_path / "project"
    work.mkdir()
    (tmp_path / "outside.html").write_text(page("<p>outside</p>"))
    (tmp_path / "outside.md").write_text("Outside.\n")
    pages = "" if surfaces.startswith("pages") else 'pages = ["index.html"]\n'
    project(work, AUDIENCE.format(grade=2) + f"[surfaces]\n{pages}{surfaces}\n", index_html=page("<p>hi</p>"))
    done = run(script, work)
    assert done.returncode == 1, done.stdout
    assert "Traceback" not in done.stdout + done.stderr
    assert done.stdout.startswith("FAIL") and named in done.stdout


def test_a_text_glob_that_reaches_outside_through_a_symlink_fails(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "notes.md").write_text(HARD)
    project(work, AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["docs/*.md"]\n')
    (work / "docs").symlink_to(tmp_path / "secret")
    done = run("ux_reading_level.py", work)
    assert done.returncode == 1
    assert "outside the project" in done.stdout and "Traceback" not in done.stderr


UNRESOLVED_TEXT = ["missing_literal", "unmatched_glob", "escaping_symlink"]


@pytest.mark.parametrize(
    "script", ["ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py"]
)
@pytest.mark.parametrize("case", UNRESOLVED_TEXT)
def test_declared_text_is_validated_before_any_script_decides(tmp_path, script, case):
    work = tmp_path / "project"
    work.mkdir()
    entry = {"missing_literal": "missing.md", "unmatched_glob": "docs/*.txt", "escaping_symlink": "docs/*.md"}
    project(work, AUDIENCE.format(grade=2) + f'[surfaces]\ntext = ["{entry[case]}"]\n')
    (work / "ux-evidence").mkdir()
    (work / "ux-evidence" / "WALKTHROUGH.md").write_text(
        "The task: create a world and walk into its first room.\n\n> The room is dark.\n"
    )
    if case == "escaping_symlink":
        (tmp_path / "secret").mkdir()
        (tmp_path / "secret" / "notes.md").write_text(HARD)
        (work / "docs").symlink_to(tmp_path / "secret")
    done = run(script, work)
    assert done.returncode != 0, done.stdout
    assert "surfaces.text" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr
    if case == "escaping_symlink":
        assert "outside the project" in done.stdout


# --- which projects are human-facing ---


@pytest.mark.parametrize(
    "where",
    [
        "tests__fixtures__sample_html",
        "venv__lib__site_html",
        "htmlcov__index_html",
        "docs___build__html__index_html",
        "build__lib__page_html",
    ],
)
def test_generated_and_vendored_html_does_not_make_a_library_human_facing(tmp_path, where):
    project(tmp_path, None, **{where: page("<p>generated</p>")})
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "lib"\nversion = "0"\n')
    for script in ("ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py"):
        done = run(script, tmp_path)
        assert done.returncode == 0, (script, done.stdout)
        assert done.stdout.startswith("NOT APPLICABLE:"), (script, done.stdout)


def test_a_virtualenv_under_any_name_is_skipped(tmp_path):
    project(tmp_path, None, myenv__lib__x_html=page("<p>x</p>"))
    (tmp_path / "myenv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert run("ux_audience.py", tmp_path).stdout.startswith("NOT APPLICABLE:")


def test_a_product_toml_with_only_a_brand_is_not_applicable_in_every_script(tmp_path):
    project(tmp_path, '[brand]\nfile = "none"\n')
    for script in ("ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py"):
        done = run(script, tmp_path)
        assert done.returncode == 0, (script, done.stdout)
        assert done.stdout.startswith("NOT APPLICABLE:"), (script, done.stdout)


def half_of_hard() -> str:
    """Return about 54 hard words: under the floor alone, over it when counted twice."""
    return ". ".join(HARD.split(". ")[:2]) + "."


def test_a_text_file_counts_once_however_many_patterns_match_it(tmp_path):
    toml = AUDIENCE.format(grade=0) + '[surfaces]\ntext = ["a.md", "a.md", "*.md"]\n'
    project(tmp_path, toml, a_md=half_of_hard())
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert re.match(r"NOT APPLICABLE: only \d+ words", done.stdout.splitlines()[0]), done.stdout


def test_a_page_matched_by_a_text_glob_counts_once(tmp_path):
    toml = AUDIENCE.format(grade=0) + '[surfaces]\npages = ["index.html"]\ntext = ["*.html"]\n'
    project(tmp_path, toml, index_html=page(f"<p>{half_of_hard()}</p>"))
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert re.match(r"NOT APPLICABLE: only \d+ words", done.stdout.splitlines()[0]), done.stdout


# --- reading level: Markdown structure, and pages without a browser ---

BULLETS = (
    "# How to use the app\n\nHere is what to do. It is easy. Read each step.\n\n"
    + "".join(f"- Tap the {c} button\n" for c in "red blue green big small top low left right new".split())
    + "".join(f"- Tap the {c} button\n" for c in "old next back home help stop".split())
    + "\n| Name | Cost | Time |\n|---|---|---|\n"
    + "".join(f"| Thing {i} | {i} | {i} min |\n" for i in range(10))
)


def test_markdown_list_items_table_rows_and_headings_are_sentences_of_their_own(tmp_path):
    project(tmp_path, AUDIENCE.format(grade=3) + '[surfaces]\ntext = ["report.md"]\n', report_md=BULLETS)
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS: reading grade")


def test_markdown_keeps_link_text_and_drops_code_images_and_tags(tmp_path):
    text = (
        "Read the [short guide](https://example.com/a-very-long-address) first.\n"
        "Run `configure --with-extraordinarily-complicated-option` now.\n\n"
        "![an illustration of considerable complexity](x.png) <span>Done.</span>\n\n"
        "~~~~\nunreadable incomprehensible\n~~~\nstill code\n~~~~\nAfter.\n"
    )
    (tmp_path / "a.md").write_text(text)
    module = load("ux_reading_level")
    passages = [t for _, t in module.extract_markdown_passages(Path(tmp_path / "a.md"))]
    assert passages == ["Read the short guide first. Run now.", "Done.", "After."]


def markdown_of(tmp_path: Path, text: str) -> list[str]:
    """Return the passages the reading-level check reads from a Markdown file holding `text`."""
    (tmp_path / "a.md").write_text(text)
    return [t for _, t in load("ux_reading_level").extract_markdown_passages(Path(tmp_path / "a.md"))]


JARGON = "Comprehensive enterprise-grade orchestration of heterogeneous interoperability"


def test_markdown_front_matter_is_not_prose(tmp_path):
    assert markdown_of(tmp_path, f"---\ntitle: x\ndescription: {JARGON}\n---\n\nThe cat sat.\n") == [
        "The cat sat."
    ]
    assert markdown_of(tmp_path, f"--- \ndescription: {JARGON}\n...\nThe cat sat.\n") == ["The cat sat."]
    assert markdown_of(tmp_path, f'+++\ntitle = "x"\ndescription = "{JARGON}"\n+++\nThe cat sat.\n') == [
        "The cat sat."
    ]
    assert markdown_of(tmp_path, "Title\n---\n\nBody.\n") == ["Title", "Body."]
    assert markdown_of(tmp_path, "Body.\n\n---\n\nMore.\n") == ["Body.", "More."]


def test_markdown_indented_code_is_not_prose_but_continuations_and_list_paragraphs_are(tmp_path):
    assert markdown_of(tmp_path, f"Intro.\n\n    {JARGON}\n\n    more_code()\nAfter.\n") == [
        "Intro.",
        "After.",
    ]
    assert markdown_of(tmp_path, f"Intro.\n\n\t{JARGON}\nAfter.\n") == ["Intro.", "After."]
    assert markdown_of(tmp_path, f"    {JARGON}\n") == []
    assert markdown_of(tmp_path, "The dog ran.\n    lazy line\n") == ["The dog ran. lazy line"]
    assert markdown_of(tmp_path, "- item\n\n    item paragraph\n") == ["item", "item paragraph"]
    assert markdown_of(tmp_path, "- item\n\n        code_in_list()\n") == ["item"]
    assert markdown_of(tmp_path, "1. item\n\n   item paragraph\n\nAfter.\n\n    code()\n") == [
        "item",
        "item paragraph",
        "After.",
    ]


def test_markdown_multiline_html_comments_are_not_prose(tmp_path):
    assert markdown_of(tmp_path, f"<!-- a\n{JARGON}\nlong comment -->\nAfter.\n") == ["After."]
    assert markdown_of(tmp_path, f"<!--\n{JARGON}\n--> Shown.\n") == ["Shown."]
    assert markdown_of(tmp_path, f"Before.\n\n<!-- {JARGON} -->\nAfter.\n") == ["Before.", "After."]
    assert markdown_of(tmp_path, "<!-- x --> Shown.\nAfter.\n\nLast.\n") == ["Shown.", "After.", "Last."]


def test_reading_level_ignores_markdown_code_and_front_matter(tmp_path):
    code = "    instantiation = orchestration.deserialization(configuration_parameterization)\n" * 10
    text = f"---\ndescription: {JARGON}\n---\n\n{EASY}\n\n{code}"
    project(tmp_path, AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["a.md"]\n', a_md=text)
    done = run("ux_reading_level.py", tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS: reading grade"), done.stdout


def run_without_node(project_dir: Path) -> subprocess.CompletedProcess[str]:
    empty = project_dir / "empty-bin"
    empty.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(CHECKS / "ux_reading_level.py")],
        cwd=project_dir, capture_output=True, text=True, timeout=60, env={"PATH": str(empty)},
    )  # fmt: skip


def test_without_node_a_script_filled_page_is_never_read_from_its_source(tmp_path):
    # The source is easy text; a script swaps in very hard text. Reading the source would pass it.
    body = f"<div id=r>{EASY}</div><script>document.getElementById('r').textContent = {HARD!r};</script>"
    toml = AUDIENCE.format(grade=2) + '[surfaces]\npages = ["index.html"]\n'
    project(tmp_path, toml, index_html=page(body))
    done = run_without_node(tmp_path)
    assert done.returncode == 127, done.stdout
    assert "node is not on PATH" in done.stdout
    assert "PASS" not in done.stdout and "NOT APPLICABLE" not in done.stdout


def test_without_node_a_url_page_exits_127_not_applicable(tmp_path):
    project(tmp_path, AUDIENCE.format(grade=2) + '[surfaces]\npages = ["http://127.0.0.1:9/"]\n')
    done = run_without_node(tmp_path)
    assert done.returncode == 127, done.stdout
    assert "node is not on PATH" in done.stdout


def test_text_only_products_are_measured_without_node(tmp_path):
    project(tmp_path, AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["a.md"]\n', a_md=HARD)
    done = run_without_node(tmp_path)
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL: reading grade")


# --- walkthrough: images must be files the reviewer has ---


@pytest.mark.parametrize(
    "links",
    [
        "![a](https://example.com/x@320x640.png) ![b](data:image/png;base64,AA@1280x800.png)",
        "![a]({outside}/shot@320x640.png) ![b](../../shot@1280x800.png)",
        "![a](index@320x640.png) ![b](index@320x640.png?@1280x800)",
        "![a](index@320x640.png) ![b](index@320x640.png#@1280x800.png)",
    ],
)
def test_walkthrough_images_must_be_distinct_files_inside_the_project(tmp_path, links):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for name in ("shot@320x640.png", "shot@1280x800.png"):
        write_screenshot(outside / name)
        write_screenshot(tmp_path / name)
    work = tmp_path / "a" / "project"
    work.mkdir(parents=True)
    text = "The task: create a world and walk into its first room.\n\n" + links.format(outside=outside)
    done = run("ux_walkthrough.py", walk_project(work, walkthrough=text))
    assert done.returncode == 1, done.stdout
    assert "viewport: no linked image for the largest viewport 1280x800" in done.stdout


def test_walkthrough_link_query_and_fragment_are_ignored_for_existence(tmp_path):
    text = WALK_TEXT.replace("index@320x640.png", "index@320x640.png?v=2").replace(
        "index@1280x800.png", "index@1280x800.png#top"
    )
    done = run("ux_walkthrough.py", walk_project(tmp_path, walkthrough=text))
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize("content", [b"", b"png", png(320, 640)[:60]], ids=["empty", "text", "truncated"])
def test_walkthrough_rejects_empty_or_undecodable_screenshots(tmp_path, content):
    root = walk_project(tmp_path)
    (root / "ux-evidence/index@320x640.png").write_bytes(content)
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@320x640.png is not a screenshot" in done.stdout
    assert "PASS" not in done.stdout


def test_walkthrough_names_the_reason_a_screenshot_does_not_decode(tmp_path):
    root = walk_project(tmp_path)
    (root / "ux-evidence/index@320x640.png").write_bytes(b"")
    assert "not a PNG (0 bytes)" in run("ux_walkthrough.py", root).stdout
    (root / "ux-evidence/index@320x640.png").write_bytes(png(320, 640)[:60])
    assert "truncated" in run("ux_walkthrough.py", root).stdout


def test_walkthrough_rejects_pixel_data_that_does_not_match_the_header(tmp_path):
    root = walk_project(tmp_path)
    forged = bytearray(png(320, 900))
    forged[16:24] = struct.pack(">II", 1280, 800)  # IHDR width and height
    forged[29:33] = struct.pack(">I", zlib.crc32(bytes(forged[12:29])))  # a true CRC: only the size lies
    (root / "ux-evidence/index@1280x800.png").write_bytes(bytes(forged))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "pixel data does not inflate" in done.stdout


def test_walkthrough_rejects_a_screenshot_with_a_filter_type_no_decoder_knows(tmp_path):
    root = walk_project(tmp_path)
    good = png(320, 640)
    rows = bytearray(zlib.decompress(good[41:-16]))  # the one IDAT body, between IHDR and IEND
    rows[0] = 255  # the first scanline's filter type; a decoder knows only 0..4
    idat = zlib.compress(bytes(rows))
    forged = (
        good[:33]
        + struct.pack(">I", len(idat))
        + b"IDAT"
        + idat
        + struct.pack(">I", zlib.crc32(b"IDAT" + idat))
        + good[-12:]
    )
    (root / "ux-evidence/index@320x640.png").write_bytes(forged)
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@320x640.png is not a screenshot" in done.stdout
    assert "filter type 255" in done.stdout


@pytest.mark.parametrize("offset", [29, -1], ids=["IHDR", "IEND"])
def test_walkthrough_rejects_a_screenshot_with_a_bad_chunk_crc(tmp_path, offset):
    root = walk_project(tmp_path)
    broken = bytearray(png(1280, 800))
    broken[offset] ^= 0xFF  # one byte of a chunk's CRC
    (root / "ux-evidence/index@1280x800.png").write_bytes(bytes(broken))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@1280x800.png is not a screenshot" in done.stdout
    assert "bad CRC" in done.stdout


@pytest.mark.parametrize(
    ("depth", "colour"), [(3, 0), (4, 2), (16, 3), (2, 6)], ids=["grey-3", "rgb-4", "palette-16", "rgba-2"]
)
def test_walkthrough_rejects_a_screenshot_whose_depth_its_colour_type_does_not_allow(tmp_path, depth, colour):
    root = walk_project(tmp_path)
    forged = bytearray(png(320, 640))
    forged[24:26] = bytes((depth, colour))  # IHDR bit depth and colour type
    forged[29:33] = struct.pack(">I", zlib.crc32(bytes(forged[12:29])))
    (root / "ux-evidence/index@320x640.png").write_bytes(bytes(forged))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@320x640.png is not a screenshot" in done.stdout


def test_walkthrough_rejects_a_screenshot_whose_size_is_not_its_viewport(tmp_path):
    root = walk_project(tmp_path)
    (root / "ux-evidence/index@1280x800.png").write_bytes(png(100, 100))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@1280x800.png is 100x100 px" in done.stdout
    assert "viewport: no linked image for the largest viewport 1280x800" in done.stdout


def test_walkthrough_accepts_a_full_page_screenshot_taller_than_its_viewport(tmp_path):
    root = walk_project(tmp_path)
    (root / "ux-evidence/index@320x640.png").write_bytes(png(320, 2000))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 0, done.stdout


def test_walkthrough_rejects_a_screenshot_wider_than_its_viewport(tmp_path):
    root = walk_project(tmp_path)
    (root / "ux-evidence/index@320x640.png").write_bytes(png(2000, 640))
    done = run("ux_walkthrough.py", root)
    assert done.returncode == 1, done.stdout
    assert "index@320x640.png is 2000x640 px" in done.stdout
    assert "scrolls sideways" in done.stdout


# --- the standards directory stays clean ---


def test_running_the_scripts_leaves_no_bytecode_in_the_checks_directory(tmp_path):
    checks = tmp_path / "checks"
    shutil.copytree(CHECKS, checks, ignore=shutil.ignore_patterns("__pycache__"))
    work = tmp_path / "project"
    work.mkdir()
    project(work, AUDIENCE.format(grade=2) + '[surfaces]\ntext = ["r.md"]\n', r_md=EASY)
    for script in ("ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py", "ux_cli.py"):
        subprocess.run([sys.executable, str(checks / script)], cwd=work, capture_output=True, timeout=60)
    assert not (checks / "__pycache__").exists()


# --- the declared command-line tool is run ---

#: A tiny real CLI. MODE picks the misbehaviour; argv is echoed so tests can prove nothing is split twice.
TOOL = """\
import os, subprocess, sys, time
MODE = {mode!r}
args = sys.argv[1:]
if MODE == "sleep":
    with open("pids", "w") as out:
        out.write(str(os.getpid()) + " " + str(subprocess.Popen(["sleep", "300"]).pid))
    time.sleep(300)
if args == ["--help"]:
    if MODE == "empty-help":
        sys.exit(0)
    if MODE == "help-fails":
        print("usage: tool [options]")
        sys.exit(2)
    if MODE == "nonsense-help":
        print("banana")
        sys.exit(0)
    print("usage: tool [options]")
    sys.exit(0)
if MODE == "accepts-bad":
    print("ok", args)
    sys.exit(0)
if MODE == "silent-bad":
    sys.exit(2)
if MODE == "nonsense-error":
    print("bad", file=sys.stderr)
    sys.exit(2)
if args and args[0].startswith("--no-such"):
    print("tool: unknown option " + args[0] + "; run tool --help", file=sys.stderr)
    sys.exit(2)
print(repr(args))
"""
CLI_TOML = AUDIENCE.format(grade=6) + "[surfaces]\n{surfaces}\n"


def cli_tool(tmp_path: Path, mode: str = "good", name: str = "tool.py") -> str:
    """Write a fixture CLI into tmp_path and return the command that runs it."""
    (tmp_path / name).write_text(TOOL.format(mode=mode))
    return f"{shlex.quote(sys.executable)} {name}"


def declared_cli_project(
    tmp_path: Path, commands: list[str], invalid: str = '["--no-such-option"]', main: str = '["--version"]'
) -> Path:
    surfaces = f"cli = {json.dumps(commands)}"
    if invalid:
        surfaces += f"\ncli_invalid = {invalid}"
    if main:
        surfaces += f"\ncli_main = {main}"
    return project(tmp_path, CLI_TOML.format(surfaces=surfaces))


def test_cli_with_help_and_a_rejected_invalid_invocation_passes(tmp_path):
    done = run("ux_cli.py", declared_cli_project(tmp_path, [cli_tool(tmp_path)]))
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS")


def test_cli_declared_as_a_string_is_accepted(tmp_path):
    command = cli_tool(tmp_path)
    toml = CLI_TOML.format(
        surfaces=f'cli = {json.dumps(command)}\ncli_invalid = ["--no-such-option"]\ncli_main = ["--version"]'
    )
    done = run("ux_cli.py", project(tmp_path, toml))
    assert done.returncode == 0, done.stdout


def test_cli_check_is_not_applicable_without_a_cli(tmp_path):
    done = run(
        "ux_cli.py", project(tmp_path, AUDIENCE.format(grade=6) + '[surfaces]\ntext = ["r.md"]\n', r_md=EASY)
    )
    assert done.returncode == 0
    assert done.stdout.startswith("NOT APPLICABLE:")


def test_cli_check_is_not_applicable_without_any_surface(tmp_path):
    done = run("ux_cli.py", tmp_path)
    assert done.returncode == 0
    assert done.stdout.startswith("NOT APPLICABLE:")


@pytest.mark.parametrize(
    ("mode", "needle"),
    [
        ("empty-help", "--help"),
        ("help-fails", "exit 2"),
        ("accepts-bad", "--no-such-option"),
        ("silent-bad", "stderr"),
        ("nonsense-help", "usage"),
        ("nonsense-error", "rejected argument"),
    ],
)
def test_cli_fails_for_each_misbehaviour(tmp_path, mode, needle):
    done = run("ux_cli.py", declared_cli_project(tmp_path, [cli_tool(tmp_path, mode)]))
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL")
    assert needle in done.stdout
    assert "tool.py" in done.stdout


def own_cli_project(
    tmp_path: Path, script: str, invalid: str = '["--invalid"]', main: str = '["--version"]'
) -> Path:
    """A project whose declared CLI is exactly `script`, for tests that need a specific help or error text."""
    (tmp_path / "tool.py").write_text(script)
    command = f"{shlex.quote(sys.executable)} tool.py"
    return declared_cli_project(tmp_path, [command], invalid, main)


def test_cli_that_prints_banana_for_help_and_bad_for_errors_fails(tmp_path):
    script = (
        "import sys\n"
        "if sys.argv[1:] == ['--help']:\n    print('banana')\n    sys.exit(0)\n"
        "print('bad', file=sys.stderr)\nsys.exit(1)\n"
    )
    done = run("ux_cli.py", own_cli_project(tmp_path, script))
    assert done.returncode == 1, done.stdout
    assert "FAIL" in done.stdout


GO_STYLE = (
    "import sys\n"
    "if sys.argv[1:] == ['--version']:\n    print('tool 1')\n    sys.exit(0)\n"
    "if sys.argv[1:] == ['--help']:\n    print('Usage of tool:', file=sys.stderr)\n    sys.exit(0)\n"
    "print('flag provided but not defined: ' + sys.argv[1], file=sys.stderr)\nsys.exit(2)\n"
)
OPTIONS_ONLY = (
    "import sys\n"
    "if sys.argv[1:] == ['--version']:\n    print('tool 1')\n    sys.exit(0)\n"
    "if sys.argv[1:] == ['--help']:\n    print('Options:\\n  -h, --help  show this')\n    sys.exit(0)\n"
    "print('unknown option: ' + sys.argv[1], file=sys.stderr)\nsys.exit(2)\n"
)
POINTS_TO_HELP = (
    "import sys\n"
    "if sys.argv[1:] == ['--version']:\n    print('tool 1')\n    sys.exit(0)\n"
    "if sys.argv[1:] == ['--help']:\n    print('Usage: tool')\n    sys.exit(0)\n"
    "print(\"Try 'tool --help'\", file=sys.stderr)\nsys.exit(2)\n"
)


STYLES = [GO_STYLE, OPTIONS_ONLY, POINTS_TO_HELP]


@pytest.mark.parametrize("script", STYLES, ids=["go", "options", "try-help"])
def test_cli_help_and_errors_in_other_common_styles_pass(tmp_path, script):
    done = run("ux_cli.py", own_cli_project(tmp_path, script))
    assert done.returncode == 0, done.stdout


def test_cli_failure_shows_exit_code_and_an_output_excerpt(tmp_path):
    done = run("ux_cli.py", declared_cli_project(tmp_path, [cli_tool(tmp_path, "help-fails")]))
    assert "exit 2" in done.stdout
    assert "usage: tool [options]" in done.stdout


def test_cli_with_several_commands_names_the_bad_one(tmp_path):
    good = cli_tool(tmp_path, "good", "good.py")
    bad = cli_tool(tmp_path, "accepts-bad", "bad.py")
    done = run("ux_cli.py", declared_cli_project(tmp_path, [good, bad]))
    assert done.returncode == 1
    assert "bad.py" in done.stdout
    assert "good.py" not in done.stdout.replace("PASS", "")


def test_cli_missing_executable_fails_naming_it(tmp_path):
    done = run("ux_cli.py", declared_cli_project(tmp_path, ["no-such-tool-here"]))
    assert done.returncode == 1, done.stdout
    assert "no-such-tool-here" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr


def test_cli_runs_in_the_project_directory(tmp_path):
    (tmp_path / "bin").mkdir()
    tool = tmp_path / "bin" / "tool"
    tool.write_text(f"#!{sys.executable}\n" + TOOL.format(mode="good"))
    tool.chmod(0o755)
    done = run("ux_cli.py", declared_cli_project(tmp_path, ["bin/tool"]))
    assert done.returncode == 0, done.stdout


def test_cli_arguments_are_passed_literally_with_no_shell(tmp_path):
    invalid = ["--no-such-x ; touch pwned", "$(touch pwned2)", "a|b", "two words"]
    declared_cli_project(tmp_path, [cli_tool(tmp_path, "accepts-bad")], json.dumps(invalid))
    done = run("ux_cli.py", tmp_path)
    assert done.returncode == 1  # accepted bad input
    for literal in invalid:
        assert literal in done.stdout
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()


def test_cli_timeout_fails_without_hanging_and_leaves_no_process(tmp_path, monkeypatch, capsys):
    module = load("ux_cli")
    monkeypatch.setattr(module, "COMMAND_SECONDS", 1)
    monkeypatch.chdir(declared_cli_project(tmp_path, [cli_tool(tmp_path, "sleep")]))
    started = time.monotonic()
    assert module.main() == 1
    assert time.monotonic() - started < 10
    out = capsys.readouterr().out
    assert "timed out" in out and "tool.py" in out
    pids = [int(p) for p in (tmp_path / "pids").read_text().split()]
    time.sleep(0.5)
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize(
    "surfaces",
    [
        'cli = "t"\ncli_invalid = "--x"',
        'cli = "t"\ncli_invalid = [1]',
        'cli = "t"\ncli_invalid = []',
        'cli = "t"\ncli_invalid = [""]',
        'cli = "t"\ncli_invalid = ["--x"]\ncli_main = "x"',
        'cli = "t"\ncli_invalid = ["--x"]\ncli_main = [1]',
    ],
)
def test_malformed_cli_invalid_fails_in_every_script(tmp_path, surfaces):
    project(tmp_path, CLI_TOML.format(surfaces=surfaces))
    key = "surfaces.cli_main" if "cli_main" in surfaces else "surfaces.cli_invalid"
    for script in ("ux_cli.py", "ux_audience.py", "ux_walkthrough.py"):
        done = run(script, tmp_path)
        assert done.returncode == 1, (script, done.stdout)
        assert key in done.stdout


def test_cli_without_cli_invalid_fails_naming_the_key(tmp_path):
    done = run("ux_cli.py", declared_cli_project(tmp_path, [cli_tool(tmp_path)], invalid=""))
    assert done.returncode == 1
    assert "surfaces.cli_invalid" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr


def test_cli_without_cli_main_fails_naming_the_key(tmp_path):
    done = run("ux_cli.py", declared_cli_project(tmp_path, [cli_tool(tmp_path)], main=""))
    assert done.returncode == 1
    assert "surfaces.cli_main" in done.stdout
    assert "--version" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr


def test_an_empty_cli_main_is_accepted_and_runs_the_bare_command(tmp_path):
    script = (
        "import sys\n"
        "if sys.argv[1:] == ['--help']:\n    print('usage: tool')\n    sys.exit(0)\n"
        "if sys.argv[1:]:\n    print('tool: unknown ' + sys.argv[1] + '; try --help', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "open('bare-ran', 'w').close()\n"
    )
    done = run("ux_cli.py", own_cli_project(tmp_path, script, main="[]"))
    assert done.returncode == 0, done.stdout
    assert (tmp_path / "bare-ran").exists()


COLOUR = "'\\x1b[31mred\\x1b[0m'"
HELP_USAGE = "if sys.argv[1:] == ['--help']:\n    print({help})\n    sys.exit(0)\n"
REJECT = (
    "if sys.argv[1:]:\n"
    "    print('tool: unknown ' + sys.argv[1] + '; try --help', file=sys.stderr{extra})\n"
    "    sys.exit(2)\n"
)


def tool_script(help: str = "'usage: tool'", reject_extra: str = "", main: str = "pass") -> str:
    """A fixture CLI whose help text, rejection stderr and bare run are chosen by the test."""
    return (
        "import os, sys\n"
        + HELP_USAGE.format(help=help)
        + "if sys.argv[1:] == ['--version']:\n    print('tool 1')\n    sys.exit(0)\n"
        + REJECT.format(extra=reject_extra)
        + main
        + "\n"
    )


def test_cli_colour_in_the_piped_main_run_fails(tmp_path):
    script = tool_script(main=f"print({COLOUR})")
    done = run("ux_cli.py", own_cli_project(tmp_path, script, main="[]"))
    assert done.returncode == 1, done.stdout
    assert "tool.py" in done.stdout
    assert "escape" in done.stdout


@pytest.mark.parametrize(
    "script",
    [
        tool_script(help=f"'usage: ' + {COLOUR}"),
        tool_script(reject_extra="").replace("; try --help'", f"; try --help ' + {COLOUR}"),
    ],
    ids=["help-stdout", "invalid-stderr"],
)
def test_cli_colour_in_help_or_error_output_fails(tmp_path, script):
    done = run("ux_cli.py", own_cli_project(tmp_path, script))
    assert done.returncode == 1, done.stdout
    assert "escape" in done.stdout


@pytest.mark.parametrize(
    ("main", "expected"),
    [
        ("sys.stdout.write('\\rworking...')", 1),
        ("sys.stdout.write('line\\r\\n')", 0),
    ],
    ids=["bare-cr", "crlf"],
)
def test_cli_carriage_return_animation_fails_but_crlf_passes(tmp_path, main, expected):
    done = run("ux_cli.py", own_cli_project(tmp_path, tool_script(main=main), main="[]"))
    assert done.returncode == expected, done.stdout
    if expected:
        assert "carriage return" in done.stdout


def test_cli_that_colours_only_a_terminal_passes(tmp_path):
    main = f"if sys.stdout.isatty():\n    print({COLOUR})\nelse:\n    print('plain')"
    done = run("ux_cli.py", own_cli_project(tmp_path, tool_script(main=main), main="[]"))
    assert done.returncode == 0, done.stdout


def test_cli_check_ignores_the_environments_colour_switches(tmp_path, monkeypatch):
    honours_no_color = tool_script(main=f"print('plain' if 'NO_COLOR' in os.environ else {COLOUR})")
    monkeypatch.setenv("NO_COLOR", "1")
    done = run("ux_cli.py", own_cli_project(tmp_path, honours_no_color, main="[]"))
    assert done.returncode == 1, done.stdout
    assert "escape" in done.stdout

    honours_force = tool_script(main=f"print({COLOUR} if 'FORCE_COLOR' in os.environ else 'plain')")
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    done = run("ux_cli.py", own_cli_project(tmp_path, honours_force, main="[]"))
    assert done.returncode == 0, done.stdout


def test_cli_check_sets_a_colour_capable_terminal_type(tmp_path, monkeypatch):
    needs_term = tool_script(
        main=f"print({COLOUR} if os.environ.get('TERM') == 'xterm-256color' else 'plain')"
    )
    monkeypatch.delenv("TERM", raising=False)
    done = run("ux_cli.py", own_cli_project(tmp_path, needs_term, main="[]"))
    assert done.returncode == 1, done.stdout


def test_cli_main_invocation_must_exit_zero(tmp_path):
    script = tool_script().replace("print('tool 1')\n    sys.exit(0)", "print('tool 1')\n    sys.exit(3)")
    done = run("ux_cli.py", own_cli_project(tmp_path, script))
    assert done.returncode == 1, done.stdout
    assert "exit 3, expected 0" in done.stdout
