"""Check that ux-evidence/WALKTHROUGH.md exists, names the main task, and shows what the user sees.

A product with pages (declared, or UI files) shows it in screenshots, which must include the smallest and the
largest declared viewport, named as the render script writes them (`index.html@320x640.png`). A CLI or text
product has no viewports: a terminal transcript (a fenced code block with a line in it), a quoted passage (a
`>` blockquote with a word in it) or a screenshot will do. Any image linked must exist, as a relative path to
a file inside the project: the reviewer has only the project, so a URL, a data: image or a file elsewhere on
this machine is not evidence and does not count. A `?query` or `#fragment` on a link is ignored. Every linked
image must be a PNG a decoder would accept (chunk CRCs, header, pixel data and scanline filters are checked),
and a screenshot named for a viewport `WxH` counts for it only when it is W px wide and at least H px tall
(the render script takes full-page shots at scale 1). The check measures the content of the evidence, never
its name. Whether the walkthrough is honest and complete is the reviewer's to judge.

Exit 0: pass, or NOT APPLICABLE (no human-facing surface). Exit 1: fail. Run from the project directory.
"""

import math
import re
import sys
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# The standards directory is not the project's: importing a sibling must not leave __pycache__ in it.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ux_common as common  # type: ignore[import-not-found]  # noqa: E402 -- this directory is put on sys.path above

WALKTHROUGH = Path("ux-evidence/WALKTHROUGH.md")
MIN_IMAGES = 2
# Markdown ![alt](path "title") and <img src="path">; a path in <angle brackets> may hold spaces.
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^)\s]+))")
FENCE_LINE = re.compile(r"^\s*(```|~~~)")
EVIDENCE_MARKUP = re.compile(r"^\s*(```|~~~|>)", re.MULTILINE)
QUOTE_WITH_WORD = re.compile(r"^\s*>.*\w", re.MULTILINE)
HTML_IMAGE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
VIEWPORT_IN_NAME = re.compile(r"@(\d+)x(\d+)(?!\d)")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Samples per pixel for each PNG colour type, and the bit depths the format allows with it (PNG spec 11.2.2).
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
# The scanline filter types a decoder knows (PNG spec 9.2); any other byte makes libpng and Pillow give up.
MAX_FILTER_TYPE = 4


def normalise(text: str) -> str:
    """Fold case and collapse whitespace, so a line break in the walkthrough does not hide the task."""
    return " ".join(text.split()).casefold()


def read_png_size(data: bytes) -> tuple[int, int] | str:
    """Return a PNG's (width, height) if a decoder would accept it, else the reason it is not one.

    What libpng and Pillow reject, this rejects: a bad chunk CRC, a header with a depth the colour type does
    not allow, a palette image with no palette, pixel data that does not inflate to the header's size, and a
    scanline whose filter type is not one of the five the format defines. Interlaced images are not supported.
    """
    if not data.startswith(PNG_SIGNATURE):
        return f"not a PNG ({len(data)} bytes)"
    pos = len(PNG_SIGNATURE)
    header: bytes | None = None
    pixels = []
    palette = ended = False
    while pos < len(data) and not ended:
        if pos + 8 > len(data):
            return "truncated"
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length or pos + 12 + length > len(data):
            return "truncated"
        crc = int.from_bytes(data[pos + 8 + length : pos + 12 + length], "big")
        if crc != zlib.crc32(kind + body):
            return f"chunk {kind.decode('ascii', 'replace')} has a bad CRC"
        if kind == b"IHDR":
            header = body
        elif kind == b"PLTE":
            palette = True
        elif kind == b"IDAT":
            pixels.append(body)
        ended = kind == b"IEND"
        pos += 12 + length
    if header is None or len(header) != 13:
        return "no valid IHDR chunk"
    if not ended:
        return "truncated"
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    depth, colour = header[8], header[9]
    compression, filter_method, interlace = header[10], header[11], header[12]
    if interlace or compression or filter_method or depth not in PNG_DEPTHS.get(colour, ()):
        return "unsupported PNG"
    if colour == 3 and not palette:
        return "palette image with no PLTE chunk"
    try:
        raw = zlib.decompress(b"".join(pixels))
    except zlib.error:
        return "pixel data does not inflate"
    row_bytes = math.ceil(width * depth * PNG_CHANNELS[colour] / 8)
    if width < 1 or height < 1 or len(raw) != height * (1 + row_bytes):
        return "pixel data does not inflate to the size in the header"
    for row, offset in enumerate(range(0, len(raw), 1 + row_bytes)):
        if raw[offset] > MAX_FILTER_TYPE:
            return f"scanline {row} has filter type {raw[offset]}, not 0..{MAX_FILTER_TYPE}"
    return width, height


def has_transcript(text: str) -> bool:
    """Say whether the text has a closed fenced block with a non-blank line in it, or a quote with a word."""
    if QUOTE_WITH_WORD.search(text):
        return True
    fence = ""
    filled = False
    for line in text.splitlines():
        marker = FENCE_LINE.match(line)
        if not fence and marker:
            fence, filled = marker.group(1), False
        elif fence and line.strip().startswith(fence):
            if filled:
                return True
            fence = ""
        elif fence and line.strip():
            filled = True
    return False


def find_image_links(text: str) -> list[str]:
    """Return the target of every image the markdown links, in order."""
    found = [(m.start(), m.group(1) or m.group(2)) for m in MARKDOWN_IMAGE.finditer(text)]
    found += [(m.start(), m.group(1) or m.group(2) or m.group(3)) for m in HTML_IMAGE.finditer(text)]
    return [link.strip() for _, link in sorted(found) if link.strip()]


def is_remote(link: str) -> bool:
    """Say whether a link points off the disk (a URL or an inline data image)."""
    return bool(re.match(r"[a-z][a-z0-9+.-]*:", link, re.IGNORECASE))


def find_local_images(root: Path, base: Path, links: list[str]) -> tuple[list[Path], list[str]]:
    """Return (the distinct image files inside the project that `links` name, one problem per other link)."""
    files: list[Path] = []
    problems = []
    for link in links:
        if is_remote(link):
            problems.append(f"image: {link[:60]} is not a file in the project, so the reviewer cannot see it")
            continue
        target = unquote(re.split(r"[?#]", link)[0])
        path = base / target
        if Path(target).is_absolute() or not common.is_inside(root, path):
            problems.append(
                f"image: {link} is outside the project; link a file inside it, by a relative path"
            )
        elif not path.is_file():
            problems.append(f"image: {link} is linked but does not exist (relative to {WALKTHROUGH.parent}/)")
        elif path.resolve() not in files:
            files.append(path.resolve())
    return files, problems


def find_image_problems(files: list[Path]) -> tuple[dict[Path, tuple[int, int]], list[str]]:
    """Return (the pixel size of each image that decodes, one problem per image that does not)."""
    sizes: dict[Path, tuple[int, int]] = {}
    problems = []
    for file in files:
        size = read_png_size(file.read_bytes())
        if isinstance(size, str):
            problems.append(f"image: {file.name} is not a screenshot: {size}")
        else:
            sizes[file] = size
    return sizes, problems


def find_viewports_shown(sizes: dict[Path, tuple[int, int]]) -> tuple[set[str], list[str]]:
    """Return (the viewports a screenshot is big enough for, one problem per screenshot that is not)."""
    shown = set()
    problems = []
    for file, (width, height) in sizes.items():
        for match in VIEWPORT_IN_NAME.finditer(file.name):
            want_width, want_height = int(match.group(1)), int(match.group(2))
            if width == want_width and height >= want_height:
                shown.add(f"{want_width}x{want_height}")
            else:
                overflow = "; a wider shot means the page scrolls sideways" if width > want_width else ""
                problems.append(
                    f"image: {file.name} is {width}x{height} px; a screenshot at {want_width}x{want_height} "
                    f"is {want_width} px wide and at least {want_height} tall{overflow}"
                )
    return shown, problems


def get_viewport_range(config: dict[str, Any] | None) -> tuple[str, str]:
    """Return the (smallest, largest) declared viewport by width, defaulting as the render check does."""
    # find_surface_problems has already failed a malformed list, so anything left is valid or absent.
    declared = common.get_surfaces_table(config).get("viewports") or common.DEFAULT_VIEWPORTS
    ordered = sorted(declared, key=lambda v: tuple(int(n) for n in str(v).split("x")))
    return str(ordered[0]), str(ordered[-1])


def find_walkthrough_problems(
    root: Path, config: dict[str, Any] | None, config_error: str, screens: bool = True
) -> list[str]:
    """Return one line per problem with the walkthrough; `screens`: the product has pages to screenshot."""
    path = root / WALKTHROUGH
    escaped = common.find_outside_problem(root, path, str(WALKTHROUGH))
    if escaped:
        return [escaped]
    if not path.is_file():
        return [f"{WALKTHROUGH}: missing; walk the main task as a first-time user and write it down there"]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return [f"{WALKTHROUGH}: is not UTF-8 text: {error}"]
    problems = []

    audience = (config or {}).get("audience")
    task = audience.get("main_task") if isinstance(audience, dict) else None
    if not isinstance(task, str) or not task.strip():
        reason = config_error or "audience.main_task is missing from product.toml"
        problems.append(f"main task: cannot compare, {reason}; fix it first (run the audience check)")
    elif normalise(task) not in normalise(text):
        problems.append(f"main task: {WALKTHROUGH} does not contain the main_task text {task.strip()!r}")

    links = find_image_links(text)
    files, link_problems = find_local_images(root, path.parent, links)
    problems += link_problems
    sizes, decode_problems = find_image_problems(files)
    problems += decode_problems
    if not screens:
        if not links and not has_transcript(text):
            if EVIDENCE_MARKUP.search(text):
                problems.append("evidence: the transcript is empty; show the command and its output")
            else:
                problems.append(
                    "evidence: no screenshot, terminal transcript (a fenced code block) or quoted passage"
                    " (a > blockquote); show the command and output, or the passage, the user sees at each"
                    " step"
                )
        return problems

    if len(files) < MIN_IMAGES:
        problems.append(
            f"images: {len(files)} linked, at least {MIN_IMAGES} needed (one screenshot per step, each a"
            " distinct file in the project)"
        )

    smallest, largest = get_viewport_range(config)
    seen, size_problems = find_viewports_shown(sizes)
    problems += size_problems
    for label, viewport in (("smallest", smallest), ("largest", largest)):
        if viewport not in seen:
            problems.append(
                f"viewport: no linked image for the {label} viewport {viewport} (name ends @{viewport}.png)"
            )
    return problems


def main() -> int:
    """Run the check in the current directory."""
    root = Path.cwd()
    config, parse_error = common.load_config(root)
    common.exit_if_malformed(config, root)
    if not common.has_surface(root, config):
        common.exit_not_applicable("no human-facing surface")
    screens = bool(common.get_declared_pages(config) or common.find_ui_files(root))
    problems = find_walkthrough_problems(root, config, parse_error, screens)
    if not problems:
        shown = "links screenshots at both ends of the viewports" if screens else "shows what the user sees"
        print(f"PASS: {WALKTHROUGH} names the main task and {shown}")
        return 0
    print(f"FAIL: {WALKTHROUGH} is incomplete")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
