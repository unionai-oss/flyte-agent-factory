"""Tests of the render check, run for real in a temp project with the fixture pages in tests/fixtures/ux/.

They need node, Playwright and Chromium, as the factory image and CI provide; without them they fail.
They never skip.
"""

import importlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/flyte_agent_factory/_assets/workspace_template/standards/checks/ux_render.py"
FIXTURES = Path(__file__).parent / "fixtures/ux"

PRODUCT = """\
[audience]
users = "Children learning to read"
age_band = "6-8"
expertise = "novice"
reading_grade = 2
main_task = "Create a world"
[surfaces]
pages = [{pages}]
{extra}"""


def make_project(
    tmp_path: Path, *names: str, extra: str = "", age_band: str = "6-8", audience_extra: str = ""
) -> Path:
    for name in names:
        shutil.copy(FIXTURES / name, tmp_path / name)
    pages = ", ".join(f'"{n}"' for n in names)
    product = PRODUCT.format(pages=pages, extra=extra)
    product = product.replace('age_band = "6-8"', f'age_band = "{age_band}"')
    (tmp_path / "product.toml").write_text(product.replace("[surfaces]", f"{audience_extra}[surfaces]"))
    return tmp_path


def render(project: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=200,
        env=env,
    )


@pytest.fixture(scope="module")
def browser_available(tmp_path_factory):
    project = make_project(tmp_path_factory.mktemp("probe"), "good.html", extra='viewports = ["320x640"]\n')
    done = render(project)
    assert done.returncode != 127, f"node, Playwright and Chromium are required here: {done.stdout.strip()}"
    return True


def findings_of(project: Path) -> list[dict[str, Any]]:
    report = json.loads((project / "ux-evidence/report.json").read_text())
    return [f for r in report["results"] for f in r["findings"]]


def test_a_good_page_passes_at_the_default_viewports_and_leaves_evidence(tmp_path, browser_available):
    project = make_project(tmp_path, "good.html")
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert "PASS: 0 findings" in done.stdout
    for viewport in ("320x640", "390x844", "1280x800"):
        assert (project / f"ux-evidence/good.html@{viewport}.png").stat().st_size > 0
        assert f"ok    good.html@{viewport}" in done.stdout
    assert len(json.loads((project / "ux-evidence/report.json").read_text())["screenshots"]) == 3


# page, the finding kind it must produce, text its message must contain
FAILURES = [
    ("low_contrast.html", "contrast", "2.32:1 < 4.5:1"),
    ("console_error.html", "console", "boom"),
    ("failed_request.html", "request", "missing.css"),
    ("overflow.html", "overflow", "320px viewport"),
    ("img_no_alt.html", "img-alt", "no alt attribute"),
    ("unnamed_button.html", "no-name", "no accessible name"),
    ("no_title_lang.html", "title", "no <title>"),
    ("no_title_lang.html", "lang", "no lang"),
    ("tiny_target.html", "target-size", "12x12px"),
]


@pytest.mark.parametrize(("page", "kind", "message"), FAILURES)
def test_each_failure_page_fails_with_its_finding_cited(tmp_path, browser_available, page, kind, message):
    # The 24px rule is measured for adults: a child audience also gets the 44px finding on these pages.
    project = make_project(tmp_path, page, extra='viewports = ["320x640", "1280x800"]\n', age_band="18-64")
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert f"FAIL  {page}@320x640" in done.stdout
    assert f"[{kind}]" in done.stdout and message in done.stdout
    assert any(f["kind"] == kind and message in f["message"] for f in findings_of(project))
    # Every other kind of finding stays quiet on a page built to show one.
    assert {f["kind"] for f in findings_of(project)} <= {kind, "title", "lang"}


# The keyboard checks run at the largest viewport only, so these pages are not flagged at the smaller one.
KEYBOARD_FAILURES = [
    ("not_reachable.html", "not-keyboard-reachable", "html > body > div"),
    ("no_focus_style.html", "no-visible-focus", "html > body > button"),
]


@pytest.mark.parametrize(("page", "kind", "selector"), KEYBOARD_FAILURES)
def test_keyboard_failures_are_found_at_the_largest_viewport_only(
    tmp_path, browser_available, page, kind, selector
):
    project = make_project(tmp_path, page, extra='viewports = ["320x640", "1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert f"ok    {page}@320x640" in done.stdout
    assert f"FAIL  {page}@1280x800" in done.stdout
    assert f"[{kind}] {selector}:" in done.stdout
    assert "checked at the largest viewport only (1280x800)" in done.stdout
    assert [(f["kind"], f["selector"]) for f in findings_of(project)] == [(kind, selector)]
    assert json.loads((project / "ux-evidence/report.json").read_text())["keyboardViewport"] == "1280x800"


def test_overflow_is_only_checked_at_320(tmp_path, browser_available):
    project = make_project(tmp_path, "overflow.html", extra='viewports = ["1280x800"]\n')
    assert render(project).returncode == 0


def test_small_targets_whose_circles_overlap_fail(tmp_path, browser_available):
    # Centres 20px apart: neither 24px circle meets the other button, but the two circles intersect.
    project = make_project(
        tmp_path, "close_targets.html", extra='viewports = ["1280x800"]\n', age_band="18-64"
    )
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert "[target-size]" in done.stdout and "10x10px" in done.stdout
    assert {f["kind"] for f in findings_of(project)} == {"target-size"}


def test_small_targets_far_enough_apart_pass(tmp_path, browser_available):
    project = make_project(
        tmp_path, "spaced_targets.html", extra='viewports = ["1280x800"]\n', age_band="18-64"
    )
    done = render(project)
    assert done.returncode == 0, done.stdout


def test_reflow_is_checked_at_the_smallest_declared_width_when_320_is_not_declared(
    tmp_path, browser_available
):
    project = make_project(tmp_path, "wide_500.html", extra='viewports = ["390x844", "1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert "FAIL  wide_500.html@390x844" in done.stdout and "390px viewport" in done.stdout
    assert "ok    wide_500.html@1280x800" in done.stdout
    assert "Reflow (no horizontal scrolling) was checked at 390px wide" in done.stdout
    assert "declare a 320x640 viewport" in done.stdout
    assert json.loads((project / "ux-evidence/report.json").read_text())["reflowWidths"] == [390]


def test_the_report_says_reflow_was_checked_at_320(tmp_path, browser_available):
    done = render(make_project(tmp_path, "good.html"))
    assert done.returncode == 0, done.stdout
    assert "Reflow (no horizontal scrolling) was checked at 320px wide\n" in done.stdout


def test_a_text_only_product_has_no_pages_to_render(tmp_path):
    (tmp_path / "product.toml").write_text(
        PRODUCT.format(pages="", extra="").replace("pages = []", 'text = ["r.md"]')
    )
    (tmp_path / "r.md").write_text("A report.\n")
    done = render(tmp_path)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("NOT APPLICABLE: no pages to render")


@pytest.mark.parametrize("surfaces", ['surfaces = "x"', '[surfaces]\nviewports = "1280x800"'])
def test_malformed_surfaces_fail_with_a_message(tmp_path, surfaces):
    (tmp_path / "product.toml").write_text(surfaces + "\n")
    done = render(tmp_path)
    assert done.returncode == 1
    assert done.stdout.startswith("FAIL") and "Traceback" not in done.stdout + done.stderr


def test_a_missing_page_fails_before_any_browser_starts(tmp_path):
    (tmp_path / "product.toml").write_text(PRODUCT.format(pages='"nope.html"', extra=""))
    (tmp_path / "other.html").write_text("<p>x</p>")
    done = render(tmp_path)
    assert done.returncode == 1 and "nope.html" in done.stdout


def test_ui_files_without_declared_pages_fail(tmp_path):
    shutil.copy(FIXTURES / "good.html", tmp_path / "good.html")
    done = render(tmp_path)
    assert done.returncode == 1
    assert "declares no pages" in done.stdout


def test_a_project_with_no_surface_is_not_applicable(tmp_path):
    (tmp_path / "tool.py").write_text("print(1)\n")
    done = render(tmp_path)
    assert done.returncode == 0
    assert done.stdout.splitlines()[0].startswith("NOT APPLICABLE:")


def test_without_node_the_check_exits_127_and_says_why(tmp_path):
    project = make_project(tmp_path, "good.html")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    done = render(project, env={**os.environ, "PATH": str(empty)})
    assert done.returncode == 127
    assert "node is not on PATH" in done.stdout


def test_without_playwright_the_check_exits_127(tmp_path, monkeypatch, capsys):
    # Neither the project nor NODE_PATH has it, and the global roots are looked up through `npm`, so hide npm
    # with an empty PATH that still has node: a directory holding only a symlink to node.
    # The fixed global root is pointed at an empty directory, in process, so a Playwright installed there on
    # this machine cannot be found either.
    node = shutil.which("node")
    assert node, "node is required here"
    project = make_project(tmp_path, "good.html")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(node)
    ux_render = load_render()
    monkeypatch.setattr(ux_render, "FALLBACK_GLOBAL_ROOT", str(tmp_path / "no-global-root"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("NODE_PATH", raising=False)
    monkeypatch.chdir(project)
    assert ux_render.main() == 127
    assert "Playwright is not installed" in capsys.readouterr().out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_is_closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_serve_and_ready_are_started_used_and_always_stopped(tmp_path, browser_available):
    port = free_port()
    serve = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
    extra = f'viewports = ["1280x800"]\nserve = "{serve}"\nready = "http://127.0.0.1:{port}/good.html"\n'
    project = make_project(tmp_path, "good.html", extra=extra)
    # The page under test is the served one: replace the declared file page with the URL.
    text = (
        (project / "product.toml")
        .read_text()
        .replace('pages = ["good.html"]', f'pages = ["http://127.0.0.1:{port}/good.html"]')
    )
    (project / "product.toml").write_text(text)
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert f"127.0.0.1:{port}/good.html@1280x800" in done.stdout
    assert port_is_closed(port)


def test_a_failing_page_over_http_still_stops_the_server(tmp_path, browser_available):
    port = free_port()
    serve = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
    extra = f'viewports = ["1280x800"]\nserve = "{serve}"\nready = "http://127.0.0.1:{port}/"\n'
    project = make_project(tmp_path, "failed_request.html", extra=extra)
    text = (
        (project / "product.toml")
        .read_text()
        .replace('"failed_request.html"', f'"http://127.0.0.1:{port}/failed_request.html"')
    )
    (project / "product.toml").write_text(text)
    done = render(project)
    assert done.returncode == 1
    assert "HTTP 404" in done.stdout and "missing.css" in done.stdout
    assert port_is_closed(port)


def test_a_server_that_never_answers_is_reported_after_the_wait(monkeypatch):
    # The wait is shortened for the test: it is the module's 60s constant that matters in production.
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        ux_render = importlib.import_module("ux_render")
    finally:
        sys.path.remove(str(SCRIPT.parent))
    monkeypatch.setattr(ux_render, "READY_SECONDS", 1)
    port = free_port()
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        problem = ux_render.wait_ready(f"http://127.0.0.1:{port}/", server)
    finally:
        server.kill()
        server.wait()
    assert "did not answer within 1s" in problem


# Each page alone at the largest viewport: the one kind of finding it must produce, and text its message has.
WIDGET_AND_CONTRAST_FAILURES = [
    ("role_slider.html", "not-keyboard-reachable", 'role="slider"'),
    ("role_link.html", "not-keyboard-reachable", 'role="link"'),
    ("tabindex_negative_link.html", "not-keyboard-reachable", "<a>"),
    ("select_no_label.html", "no-name", "<select>"),
    ("textarea_no_label.html", "no-name", "<textarea>"),
    ("button_hidden_text.html", "no-name", "<button>"),
    ("button_aria_hidden_text.html", "no-name", "<button>"),
    ("input_value_contrast.html", "contrast", '"faint text"'),
    ("placeholder_contrast.html", "contrast", "placeholder"),
    ("filter_opacity.html", "contrast", "nearly invisible"),
    ("ancestor_opacity.html", "contrast", "nearly invisible"),
    ("text_fill_color.html", "contrast", "#eeeeee on #ffffff"),
    ("sibling_backdrop.html", "contrast", "#cccccc on #ffffff"),
    ("svg_text.html", "contrast", "Sales up"),
    ("dark_scheme.html", "contrast", "dark colour scheme"),
    ("crowded_nav.html", "target-size", "under 24x24"),
]


@pytest.mark.parametrize(("page", "kind", "text"), WIDGET_AND_CONTRAST_FAILURES)
def test_widgets_fields_and_hidden_contrast_are_caught(tmp_path, browser_available, page, kind, text):
    project = make_project(tmp_path, page, extra='viewports = ["1280x800"]\n', age_band="18-64")
    done = render(project)
    assert done.returncode == 1, done.stdout
    found = findings_of(project)
    assert any(f["kind"] == kind and text in f["message"] for f in found), done.stdout
    assert {f["kind"] for f in found} == {kind}


# Correct, accessible pages that a cruder check fails.
ACCESSIBLE = [
    ("native_checkboxes.html",),  # user-agent sized checkboxes and radios: the WCAG 2.5.8 exception
    ("skip_link.html",),  # visually hidden until focused, then large enough
    ("focus_underline.html",),  # focus shown by an underline alone
    ("focus_within_parent.html",),  # focus shown on the parent
    ("inline_links_in_text.html",),  # links in a sentence
    ("shadow_ok.html",),  # a default button in a web component
    ("frame_ok_parent.html", "frame_ok.html"),  # a default button in a frame
    ("named_controls.html",),  # labels, aria-labelledby to hidden text, an icon beside text
    ("disabled_controls.html",),  # disabled, aria-disabled, inert and hidden controls need no Tab stop
    ("pseudo_ok.html",),  # generated text with enough contrast
]


@pytest.mark.parametrize("pages", ACCESSIBLE)
def test_correct_accessible_patterns_pass(tmp_path, browser_available, pages):
    # Unstyled checkboxes and radios keep the browser's size, which only the adult 24px rule accepts.
    age_band = "18-64" if pages == ("native_checkboxes.html",) else "6-8"
    project = make_project(tmp_path, *pages, extra='viewports = ["320x640", "1280x800"]\n', age_band=age_band)
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert "PASS: 0 findings" in done.stdout


@pytest.mark.parametrize(("page", "host"), [("shadow_bad.html", "x-b"), ("frame_bad_parent.html", "iframe")])
def test_open_shadow_roots_and_same_origin_frames_are_inspected(tmp_path, browser_available, page, host):
    project = make_project(tmp_path, page, extra='viewports = ["1280x800"]\n', age_band="18-64")
    shutil.copy(FIXTURES / "frame_bad.html", project / "frame_bad.html")
    done = render(project)
    assert done.returncode == 1, done.stdout
    inside = {
        f["kind"] for f in findings_of(project) if f["selector"].startswith(f"html > body > {host} >>> ")
    }
    assert inside == {"contrast", "img-alt", "no-name", "target-size", "no-visible-focus"}, done.stdout


def test_what_cannot_be_measured_is_said_not_passed_silently(tmp_path, browser_available):
    pages = ("gradient_backdrop.html", "pseudo_content.html", "cross_origin_frame.html", "shadow_closed.html")
    project = make_project(tmp_path, *pages, extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert "Not measured on these pages" in done.stdout
    assert "text over a background image or gradient: gradient_backdrop.html" in done.stdout
    assert "::before content (generated text): pseudo_content.html" in done.stdout
    assert "- contrast: text over a background image or gradient: gradient_backdrop.html" in done.stdout
    # A cross-origin frame hides more than contrast, so it is not labelled as a contrast gap.
    assert "  - a cross-origin frame: its content is not inspected: cross_origin_frame.html" in done.stdout
    assert "Not measured on any page: closed shadow roots and cross-origin frames" in done.stdout
    assert "console errors later than about 1s after the network goes quiet" in done.stdout


def contrast_findings(project: Path) -> list[dict[str, Any]]:
    return [f for f in findings_of(project) if f["kind"] == "contrast"]


def test_low_contrast_before_and_after_text_fails_naming_the_pseudo_element(tmp_path, browser_available):
    project = make_project(tmp_path, "pseudo_low_contrast.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    found = contrast_findings(project)
    assert {f["kind"] for f in findings_of(project)} == {"contrast"}
    before = [f for f in found if f["selector"].endswith("h2::before")]
    after = [f for f in found if f["selector"].endswith("p::after")]
    assert len(before) == 1 and "#eeeeee on #ffffff" in before[0]["message"], done.stdout
    assert len(after) == 1 and "#dddddd on #ffffff" in after[0]["message"], done.stdout
    assert "Faint before text" in before[0]["message"]
    assert "pseudo_low_contrast.html" in done.stdout and "1280x800" in done.stdout


def test_large_generated_text_needs_only_three_to_one(tmp_path, browser_available):
    project = make_project(tmp_path, "pseudo_large.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert "PASS: 0 findings" in done.stdout


def test_opacity_on_generated_text_lowers_its_contrast(tmp_path, browser_available):
    project = make_project(tmp_path, "pseudo_opacity.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    found = contrast_findings(project)
    assert [f["selector"].endswith("h2::before") for f in found] == [True], done.stdout


def test_generated_text_over_an_indeterminate_layer_is_listed_not_measured(tmp_path, browser_available):
    project = make_project(tmp_path, "pseudo_positioned.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 0, done.stdout
    assert "- contrast: " in done.stdout
    assert "::before content (generated text): pseudo_positioned.html" in done.stdout


@pytest.mark.parametrize(
    ("page", "host"), [("pseudo_shadow.html", "x-p"), ("pseudo_frame_parent.html", "iframe")]
)
def test_generated_text_in_shadow_roots_and_frames_is_measured(tmp_path, browser_available, page, host):
    project = make_project(tmp_path, page, extra='viewports = ["1280x800"]\n')
    shutil.copy(FIXTURES / "pseudo_frame.html", project / "pseudo_frame.html")
    done = render(project)
    assert done.returncode == 1, done.stdout
    found = contrast_findings(project)
    assert [f["selector"] for f in found if f["selector"].startswith(f"html > body > {host} >>> ")] != [], (
        done.stdout
    )
    assert all(f["selector"].endswith("::before") for f in found)


@pytest.mark.parametrize("page", ["clipped_text.html", "fixed_bar.html"])
def test_text_cut_off_at_the_reflow_width_fails(tmp_path, browser_available, page):
    project = make_project(tmp_path, page, extra='viewports = ["320x640"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert [f["kind"] for f in findings_of(project)] == ["overflow"]
    assert "cut off at the right edge" in done.stdout


def test_text_truncated_with_an_ellipsis_is_deliberate(tmp_path, browser_available):
    done = render(make_project(tmp_path, "ellipsis.html", extra='viewports = ["320x640"]\n'))
    assert done.returncode == 0, done.stdout


def test_a_console_error_soon_after_load_is_caught(tmp_path, browser_available):
    project = make_project(tmp_path, "late_console_error.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert "late boom" in done.stdout


def test_pages_whose_names_slug_alike_keep_separate_screenshots(tmp_path, browser_available):
    (tmp_path / "a").mkdir()
    shutil.copy(FIXTURES / "good.html", tmp_path / "a" / "index.html")
    shutil.copy(FIXTURES / "good.html", tmp_path / "a-index.html")
    extra = 'viewports = ["1280x800"]\n'
    (tmp_path / "product.toml").write_text(
        PRODUCT.format(pages='"a/index.html", "a-index.html"', extra=extra)
    )
    done = render(tmp_path)
    assert done.returncode == 0, done.stdout
    shots = json.loads((tmp_path / "ux-evidence/report.json").read_text())["screenshots"]
    assert len(set(shots)) == 2 and all(Path(s).is_file() for s in shots)


def test_a_broken_playwright_package_exits_127(tmp_path):
    assert shutil.which("node"), "node is required here"
    project = make_project(tmp_path, "good.html")
    broken = project / "node_modules" / "playwright"
    broken.mkdir(parents=True)
    (broken / "package.json").write_text('{"name": "playwright", "main": "missing.js"}')
    done = render(project)
    assert done.returncode == 127, done.stdout
    assert "Playwright could not be loaded" in done.stdout


def test_evidence_is_never_written_outside_the_project(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    make_project(project, "good.html")
    (project / "ux-evidence").symlink_to(elsewhere)
    done = render(project)
    assert done.returncode == 1
    assert "outside the project" in done.stdout
    assert not any(elsewhere.iterdir())


def load_render() -> Any:
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        return importlib.import_module("ux_render")
    finally:
        sys.path.remove(str(SCRIPT.parent))


def test_playwright_in_global_mcp_install_is_found(tmp_path, monkeypatch):
    global_root = tmp_path / "global" / "node_modules"
    playwright = global_root / "@playwright/mcp/node_modules/playwright"
    playwright.mkdir(parents=True)
    (playwright / "package.json").write_text('{"name": "playwright"}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(f"#!/bin/sh\nprintf '%s\\n' '{global_root}'\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("NODE_PATH", raising=False)
    ux_render = load_render()
    monkeypatch.setattr(ux_render, "FALLBACK_GLOBAL_ROOT", str(tmp_path / "missing"))
    assert ux_render.find_playwright(tmp_path / "project") == playwright


def test_the_worst_case_fits_inside_the_gate_timeout():
    # The factory kills a check at 300s; finding Playwright, the server's start and rendering must end before.
    ux_render = load_render()
    assert ux_render.NPM_SECONDS + ux_render.READY_SECONDS + ux_render.NODE_SECONDS <= 280
    assert ux_render.RENDER_BUDGET_SECONDS < ux_render.NODE_SECONDS


def test_pages_past_the_time_budget_are_reported_not_rendered(
    tmp_path, browser_available, monkeypatch, capsys
):
    ux_render = load_render()
    monkeypatch.setattr(ux_render, "RENDER_BUDGET_SECONDS", 0)
    monkeypatch.chdir(make_project(tmp_path, "good.html", extra='viewports = ["1280x800"]\n'))
    assert ux_render.main() == 1
    assert "time budget ran out" in capsys.readouterr().out


def start_served_render(tmp_path: Path) -> tuple[subprocess.Popen[str], int]:
    """Start the check on a served page that takes a while to settle; return it once the server answers."""
    port = free_port()
    serve = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
    extra = f'viewports = ["320x640", "390x844", "1280x800"]\nserve = "{serve}"\nready = "http://127.0.0.1:{port}/"\n'
    project = make_project(tmp_path, "good.html", extra=extra)
    text = (
        (project / "product.toml").read_text().replace('"good.html"', f'"http://127.0.0.1:{port}/good.html"')
    )
    (project / "product.toml").write_text(text)
    check = subprocess.Popen(
        [sys.executable, str(SCRIPT)], cwd=project, text=True, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    deadline = time.monotonic() + 60
    while port_is_closed(port):
        assert time.monotonic() < deadline, "the server never started"
        time.sleep(0.2)
    return check, port


def wait_closed(port: int) -> bool:
    deadline = time.monotonic() + 10
    while not port_is_closed(port):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def test_the_server_dies_when_the_check_is_killed_outright(tmp_path, browser_available):
    # The gate kills a check that runs too long with SIGKILL to its process group: no cleanup code runs.
    check, port = start_served_render(tmp_path)
    os.killpg(check.pid, signal.SIGKILL)
    check.wait()
    assert wait_closed(port)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_the_server_dies_when_the_check_is_stopped(tmp_path, browser_available, signum):
    check, port = start_served_render(tmp_path)
    check.send_signal(signum)
    check.wait(timeout=30)
    assert wait_closed(port)


# --- reading level of rendered pages ---

READING = PRODUCT.replace("reading_grade = 2", "reading_grade = 3")


def reading_level(project: Path) -> subprocess.CompletedProcess[str]:
    script = SCRIPT.with_name("ux_reading_level.py")
    return subprocess.run(
        [sys.executable, str(script)], cwd=project, capture_output=True, text=True, timeout=200
    )


def test_reading_level_measures_text_a_script_inserts(tmp_path, browser_available):
    project = make_project(tmp_path, "spa_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"spa_hard.html"', extra=""))
    done = reading_level(project)
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL: reading grade")
    assert "spa_hard.html (rendered)  grade " in done.stdout


def test_reading_level_skips_text_css_hides(tmp_path, browser_available):
    project = make_project(tmp_path, "css_hidden_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"css_hidden_hard.html"', extra=""))
    done = reading_level(project)
    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("PASS: reading grade")


def test_reading_level_reads_a_served_page(tmp_path, browser_available):
    port = free_port()
    serve = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
    project = make_project(tmp_path, "spa_hard.html")
    extra = f'serve = "{serve}"\nready = "http://127.0.0.1:{port}/"\n'
    pages = f'"http://127.0.0.1:{port}/spa_hard.html"'
    (project / "product.toml").write_text(READING.format(pages=pages, extra=extra))
    done = reading_level(project)
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL: reading grade")
    assert port_is_closed(port)


def test_reading_level_without_node_exits_127_for_a_script_filled_page(tmp_path):
    project = make_project(tmp_path, "spa_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"spa_hard.html"', extra=""))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    script = SCRIPT.with_name("ux_reading_level.py")
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=project, capture_output=True, text=True, timeout=60, env={"PATH": str(empty)},
    )  # fmt: skip
    assert done.returncode == 127, done.stdout
    assert "node is not on PATH" in done.stdout


def test_reading_level_with_a_broken_playwright_exits_127(tmp_path):
    project = make_project(tmp_path, "spa_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"spa_hard.html"', extra=""))
    broken = project / "node_modules" / "playwright"
    broken.mkdir(parents=True)
    (broken / "package.json").write_text('{"name": "playwright", "main": "missing.js"}')
    done = reading_level(project)
    assert done.returncode == 127, done.stdout
    assert "Playwright could not be loaded" in done.stdout


def test_reading_level_with_a_browser_that_cannot_start_exits_127(tmp_path):
    project = make_project(tmp_path, "spa_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"spa_hard.html"', extra=""))
    script = SCRIPT.with_name("ux_reading_level.py")
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=project, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "no-browsers")},
    )  # fmt: skip
    assert done.returncode == 127, done.stdout
    assert "browser could not start" in done.stdout


def test_one_page_that_fails_to_load_fails_the_check_naming_it(tmp_path, browser_available):
    project = make_project(tmp_path, "good.html")
    toml = READING.format(pages='"good.html", "http://127.0.0.1:9/gone.html"', extra="")
    (project / "product.toml").write_text(toml)
    done = reading_level(project)
    assert done.returncode == 1, done.stdout
    assert done.stdout.startswith("FAIL")
    assert "http://127.0.0.1:9/gone.html" in done.stdout


def test_a_page_that_fails_to_load_is_not_not_applicable_under_the_word_floor(tmp_path, browser_available):
    project = make_project(tmp_path, "good.html")
    toml = READING.format(pages='"good.html", "http://127.0.0.1:9/gone.html"', extra="")
    (project / "product.toml").write_text(toml)
    done = reading_level(project)
    assert "NOT APPLICABLE" not in done.stdout and done.returncode == 1


def test_the_script_filled_hard_page_fails_for_its_grade(tmp_path, browser_available):
    project = make_project(tmp_path, "spa_hard.html")
    (project / "product.toml").write_text(READING.format(pages='"spa_hard.html"', extra=""))
    done = reading_level(project)
    assert done.returncode == 1, done.stdout
    assert re.match(r"FAIL: reading grade \d", done.stdout)


# --- the age-specific target size and body text size ---

CHILD_TARGET = "under 44x44"


@pytest.mark.parametrize(
    ("page", "age_band", "audience_extra", "fails"),
    [
        ("targets_30.html", "6-8", "", True),
        ("targets_30.html", "12-14", "", True),
        ("targets_30.html", "8-15", "", True),
        ("targets_30.html", "teenagers and parents", "under_13 = true\n", True),
        ("targets_30.html", "18-64", "", False),
        ("targets_30.html", "13-17", "", False),
        ("targets_30.html", "13+", "", False),
        ("targets_30.html", "adults", "", False),
        ("targets_30.html", "teenagers and parents", "under_13 = false\n", False),
        ("targets_44.html", "6-8", "", False),
        ("targets_44.html", "18-64", "", False),
    ],
)
def test_children_under_13_need_44px_targets_and_adults_need_24px(
    tmp_path, browser_available, page, age_band, audience_extra, fails
):
    project = make_project(
        tmp_path, page, extra='viewports = ["1280x800"]\n', age_band=age_band, audience_extra=audience_extra
    )
    done = render(project)
    found = findings_of(project)
    if not fails:
        assert done.returncode == 0, done.stdout
        return
    assert done.returncode == 1, done.stdout
    assert {f["kind"] for f in found} == {"target-size-child"}
    assert len(found) == 2
    assert all(CHILD_TARGET in f["message"] and "30x30px" in f["message"] for f in found)
    assert all(f["selector"].startswith("html > body > main > button") for f in found)
    assert (
        f"FAIL  {page}@1280x800" in done.stdout
        and "[target-size-child] html > body > main > button" in done.stdout
    )


def test_a_child_target_one_pixel_short_fails_and_names_the_size(tmp_path, browser_available):
    project = make_project(tmp_path, "targets_44x43.html", extra='viewports = ["390x844"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert "44x43px" in done.stdout and "390x844" in done.stdout


def test_the_child_finding_is_separate_from_the_24px_one(tmp_path, browser_available):
    project = make_project(tmp_path, "tiny_target.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert {f["kind"] for f in findings_of(project)} == {"target-size", "target-size-child"}


def test_a_child_has_no_spacing_exception(tmp_path, browser_available):
    # Far enough apart for the 24px rule, still under 44px.
    project = make_project(tmp_path, "spaced_targets.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert {f["kind"] for f in findings_of(project)} == {"target-size-child"}


def test_unstyled_native_toggles_are_too_small_for_a_child(tmp_path, browser_available):
    project = make_project(tmp_path, "native_checkboxes.html", extra='viewports = ["1280x800"]\n')
    done = render(project)
    assert done.returncode == 1, done.stdout
    assert {f["kind"] for f in findings_of(project)} == {"target-size-child"}


def test_a_skip_link_is_judged_by_its_size_when_focused(tmp_path, browser_available):
    (tmp_path / "big").mkdir()
    (tmp_path / "small").mkdir()
    big = make_project(tmp_path / "big", "skip_link.html", extra='viewports = ["1280x800"]\n')
    assert render(big).returncode == 0
    small = make_project(tmp_path / "small", "skip_link_small.html", extra='viewports = ["1280x800"]\n')
    done = render(small)
    assert done.returncode == 1, done.stdout
    assert any(
        f["kind"] == "target-size-child" and "hidden until focused" in f["message"]
        for f in findings_of(small)
    )


def test_links_inside_a_sentence_are_exempt_for_a_child(tmp_path, browser_available):
    project = make_project(tmp_path, "inline_links_in_text.html", extra='viewports = ["1280x800"]\n')
    render(project)
    assert "target-size-child" not in {f["kind"] for f in findings_of(project)}


@pytest.mark.parametrize("page", ["native_checkboxes.html", "skip_link_small.html", "targets_30.html"])
def test_native_controls_and_the_skip_link_still_pass_for_adults(tmp_path, browser_available, page):
    project = make_project(tmp_path, page, extra='viewports = ["1280x800"]\n', age_band="18-64")
    done = render(project)
    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize(
    ("age_band", "fails"),
    [("65+", True), ("65-80", True), ("18-80", False), ("18-64", False), ("adult", False), ("6-8", False)],
)
def test_older_adults_need_body_text_of_at_least_16px(tmp_path, browser_available, age_band, fails):
    project = make_project(
        tmp_path, "small_body_text.html", extra='viewports = ["1280x800"]\n', age_band=age_band
    )
    done = render(project)
    if not fails:
        # A child audience may fail on other findings; the text rule must not be among them.
        assert "body-text-size" not in {f["kind"] for f in findings_of(project)}, done.stdout
        return
    assert done.returncode == 1, done.stdout
    found = findings_of(project)
    assert {f["kind"] for f in found} == {"body-text-size"}
    assert "13px" in found[0]["message"] and found[0]["selector"] == "html > body > main > p"


def test_body_text_of_16px_passes_and_only_running_text_is_measured(tmp_path, browser_available):
    # The 12px button label and 12px two-word span are not running text.
    project = make_project(tmp_path, "body_text_16.html", extra='viewports = ["1280x800"]\n', age_band="65+")
    done = render(project)
    assert done.returncode == 0, done.stdout
