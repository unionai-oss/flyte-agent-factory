"""Browser evidence for the UX render check: render fixture pages in Chromium and show what it saw.

Runs the template's render script for real on a clean page and on one that throws, prints each screenshot
it wrote and its console findings, and exits 0 only when the clean page passes and the error is reported.
Screenshots stay in `ux-evidence/` under a temp directory, whose path is printed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/flyte_agent_factory/_assets/workspace_template/standards/checks/ux_render.py"
FIXTURES = Path(__file__).parent / "fixtures/ux"
PRODUCT = """\
[audience]
users = "Adults planning a trip"
age_band = "18-64"
expertise = "novice"
reading_grade = 8
main_task = "Open the page"
[surfaces]
pages = ["{page}"]
"""


def render(directory: Path, page: str) -> tuple[int, str]:
    """Render one fixture page in its own project and print the evidence; return the exit code and output."""
    project = directory / Path(page).stem
    project.mkdir()
    shutil.copy(FIXTURES / page, project / page)
    (project / "product.toml").write_text(PRODUCT.format(page=page))
    done = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=project, capture_output=True, text=True, timeout=280, check=False
    )
    print(f"== {page}: exit {done.returncode}\n{done.stdout.strip()}")
    for shot in sorted((project / "ux-evidence").glob("*.png")):
        print(f"screenshot: {shot} ({shot.stat().st_size} bytes)")
    report = project / "ux-evidence/report.json"
    if report.is_file():
        for result in json.loads(report.read_text())["results"]:
            console = [f["message"] for f in result["findings"] if f["kind"] == "console"]
            print(f"console at {result['viewport']}: {'; '.join(console) or 'no errors'}")
    return done.returncode, done.stdout


def main() -> int:
    directory = Path(tempfile.mkdtemp(prefix="ux-render-evidence-"))
    print(f"evidence directory: {directory}")
    good, _ = render(directory, "good.html")
    bad, output = render(directory, "console_error.html")
    shots = list(directory.glob("*/ux-evidence/*.png"))
    ok = good == 0 and bad == 1 and "uncaught: boom" in output and len(shots) == 6
    print("PASS" if ok else "FAIL: the clean page must pass, the throwing one must report its error")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
