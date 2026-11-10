"""Demo package checks and publishing: what a demo must contain, and the artifact it ships as."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path

import flyte
from flyte.io import Dir

from flyte_agent_factory import _checkpoint, _secret_env, _session_config, _timeline
from flyte_agent_factory._errors import REMOTE_ERRORS


async def _publish_demo_reports(
    workspace: Path, summary: str, timeline: _timeline._Timeline, only: str = "", *, publish: bool = False
) -> None:
    """Put each finished demo video in the run report, playable.

    Git holds the editable package; the report is where someone checking on the
    run actually looks, and asking them to clone a repo to watch a 35-second
    video is how a demo goes unwatched.
    """
    # Only the package the brief named. With none named this is not a demo brief,
    # and walking `demo/` published other projects' videos into this run's report
    # (a project with no demo reported three of them).
    root = workspace / _session_config.DEMO_SUBDIR
    if not only or not root.is_dir():
        return
    for package in sorted(p for p in root.iterdir() if p.is_dir() and p.name == only):
        try:
            video, files = _check_demo_package(package)
            head = await _checkpoint._git_head(workspace)
            tab = flyte.report.get_tab(f"Demo · {package.name}")
            tab.replace(
                _demo_report(name=package.name, video=video, files=files, summary=summary, pushed=head)
            )
            await flyte.report.flush.aio()
            timeline.row("🎬", f"{package.name} in the report ({video.stat().st_size // 1024}KB)")
        # TypeError: a malformed manifest section (the package check); the rest is the report's own
        # storage and RPCs and the video's stat.
        except (TypeError, *REMOTE_ERRORS) as e:  # a report tab is never worth failing a run over
            timeline.row("⚠️", f"could not report {package.name}: {type(e).__name__}: {e}")
            continue
        if publish:
            # In the background: the pass is announced now, whatever the upload does.
            _publish_in_background(
                _publish_demo_artifact(workspace, package, video, head, timeline),
                timeline,
                f"publish demo-{package.name}",
            )


#: A demo package bigger than this is not published as an artifact (the repo still has it).
_DEMO_ARTIFACT_MAX_BYTES = 500 * 1024 * 1024


#: Bound on the background publish; it never holds up the pass itself.
_DEMO_ARTIFACT_TIMEOUT_S = 900


_background: set[asyncio.Task[None]] = set()


def _committed_files(workspace: Path, package: Path) -> list[Path]:
    """Return the package's files as committed to git -- never what merely sits on disk.

    The artifact store is readable project-wide, so an uncommitted `.env`, a key, a
    `.venv` or `.git` under the package must never reach it (qa on demo-artifact).
    """
    out = subprocess.run(
        # HEAD, not the index: a staged-but-uncommitted file is not committed. At a pass
        # the disk matches HEAD (the DONE preflight refuses uncommitted tracked changes).
        _secret_env.git_argv(
            "-C",
            str(workspace),
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            "HEAD",
            "--",
            str(package.relative_to(workspace)),
        ),
        env=_secret_env.child_env(),
        check=True,
        capture_output=True,
    ).stdout.decode()
    files = [workspace / rel for rel in out.split("\0") if rel]
    return [f for f in files if f.is_file() and not f.is_symlink() and "__pycache__" not in f.parts]


def _demo_bundle(workspace: Path, package: Path, video: Path, out: Path) -> Path:
    """Return a flat directory: the video, plus the committed package as a tarball.

    Flat because the artifact service's directory upload flattens subdirectories
    (probed: `a/b/deep.txt` came back as `deep.txt`), which turned a passed demo's
    `assets/` tree into one pile (a past demo). The tarball keeps the layout.
    Raises ValueError over `_DEMO_ARTIFACT_MAX_BYTES`.
    """
    import tarfile

    files = _committed_files(workspace, package)
    total = sum(f.stat().st_size for f in files)
    if total > _DEMO_ARTIFACT_MAX_BYTES:
        raise ValueError(f"package is {total // 2**20} MB, over {_DEMO_ARTIFACT_MAX_BYTES // 2**20} MB")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, out / video.name)
    with tarfile.open(out / f"{package.name}.tar.gz", "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(Path(package.name) / f.relative_to(package)), recursive=False)
    return out


async def _publish_demo_artifact(
    workspace: Path, package: Path, video: Path, head: str | None, timeline: _timeline._Timeline
) -> None:
    """Publish a passed demo as artifact `demo-<name>`, fetchable without access to the repo.

    A past demo passed review and the person who asked for it could not get
    the video: the package lived only in a repo the operator cannot clone.
    """
    from flyte import remote
    from flyte.io import Dir

    name = f"demo-{package.name}"
    try:
        with tempfile.TemporaryDirectory(prefix="demo-artifact-") as tmp:
            # Off the loop: tarring a few hundred MB stalled it for 14 s in qa's timing.
            bundle = await asyncio.to_thread(
                _demo_bundle, workspace, package, video, Path(tmp) / package.name
            )
            uri = await asyncio.to_thread(remote.upload_dir, bundle)
            ctx = flyte.ctx()
            run = ctx.action.run_name if ctx and ctx.action else ""
            await asyncio.to_thread(
                remote.Artifact.create,
                Dir.from_existing_remote(uri),
                name=name,
                version=f"{head or 'unpushed'}-{int(time.time())}",
                description=(
                    f"{video.name} and {package.name}.tar.gz (the committed package). "
                    "Passed independent review."
                )[:255],
                attrs={
                    "video": video.name,
                    "package": f"{package.name}.tar.gz",
                    "run": run,
                    "commit": head or "",
                },
            )
        timeline.row("📦", f"published artifact {name!r} (video + package tarball)")
    except (*REMOTE_ERRORS, tarfile.TarError) as e:  # publishing is a convenience; the repo has the package
        timeline.row("⚠️", f"could not publish artifact {name!r}: {type(e).__name__}: {e}")


def _publish_in_background(
    coro: Awaitable[object], timeline: _timeline._Timeline, what: str
) -> asyncio.Task[None]:
    """Run `coro` bounded, without holding up the caller; a hang becomes a timeline row."""

    async def bounded() -> None:
        try:
            await asyncio.wait_for(coro, timeout=_DEMO_ARTIFACT_TIMEOUT_S)
        except asyncio.TimeoutError:
            timeline.row("⚠️", f"{what} timed out after {_DEMO_ARTIFACT_TIMEOUT_S}s")

    task = asyncio.create_task(bounded(), name=what)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _demo_package_problem(workspace: Path, only: str = "") -> str:
    """Why the brief's demo package is not shippable, or "" if it is.

    Checks the package the brief named and nothing else. There used to be a
    fallback that inferred the package from git, and it was unsound the moment
    two agents shared a repository: a session rebased onto `main`, the pulled
    commits looked like its own changes, and it was told to fix a package
    belonging to a live session next door. It refused three times, correctly.

    With no package named there is no check here -- a brief that did not
    declare a deliverable folder is not a demo brief. The reviewers still read
    everything the brief covers.
    """
    if not only:
        return ""
    root = workspace / _session_config.DEMO_SUBDIR
    package = root / only
    if not package.is_dir():
        found = (
            ", ".join(sorted(p.name for p in root.iterdir() if p.is_dir())) if root.is_dir() else "nothing"
        )
        return f"the brief's package `{_session_config.DEMO_SUBDIR}/{only}` does not exist (found: {found})."
    try:
        _check_demo_package(package)
    except (RuntimeError, TypeError) as e:
        return f"`{_session_config.DEMO_SUBDIR}/{only}`: {e}"
    return ""


#: The artifact a demo narrates with, published by the voice factory.
_VOICE_MODEL_ARTIFACT = "voice-model"


#: Containers accepted when looking for the video, newest-wins on size. WebM is
#: still here because the raw screen capture under `assets/` is one.
_VIDEO_SUFFIXES = (".mp4", ".webm")


#: Above this, a video is linked rather than inlined. Base64 inflates by ~4/3,
#: and the report is one HTML file a browser has to pull in full before it
#: renders anything -- a 50MB inline video makes the whole report unusable
#: rather than making the video available.
_REPORT_VIDEO_MAX_BYTES = 8_000_000


def _demo_report(*, name: str, video: Path, files: list[Path], summary: str, pushed: str | None) -> str:
    """Return the run report, with the video playable in it.

    Embedded as a data URI because `flyte.report` takes HTML and nothing else:
    there is no way to attach a file beside it, and a relative URL in the
    report would not resolve to a sibling object in storage.
    """
    size = video.stat().st_size
    if size <= _REPORT_VIDEO_MAX_BYTES:
        encoded = base64.b64encode(video.read_bytes()).decode()
        mime = "video/mp4" if video.suffix.lower() == ".mp4" else "video/webm"
        player = (
            f'<video controls style="max-width:100%;border-radius:8px" '
            f'src="data:{mime};base64,{encoded}"></video>'
        )
    else:
        player = (
            f"<p><em>{html.escape(video.name)} is {size // 1_000_000}MB, too large to embed. "
            "It is in the package in git and in this run's outputs.</em></p>"
        )

    listing = "".join(f"<li><code>{html.escape(p.name)}</code></li>" for p in files if p != video)
    where = (
        f"<p>In git at <code>{html.escape(_session_config.DEMO_SUBDIR)}/{html.escape(name)}/</code>, "
        f"commit <code>{html.escape(pushed)}</code>.</p>"
        if pushed
        else "<p>Not pushed to git: this run was given a directory rather than a repository.</p>"
    )
    return (
        f"<h3>Demo · {html.escape(name)}</h3>"
        f"{player}"
        f"{where}"
        f"<p><b>Also in the package:</b></p><ul>{listing}</ul>"
        f"<p><b>What the session reported:</b></p><pre>{html.escape(summary[:4000])}</pre>"
    )


def _check_demo_package(package: Path) -> tuple[Path, list[Path]]:
    """Fail unless the package is actually shareable.

    Checked rather than trusted because every failure here is silent
    downstream: a run that exits 0 having recorded nothing is indistinguishable
    from one that worked, and the person who finds out is the one about to send
    it to a customer.
    """
    if not package.is_dir():
        raise RuntimeError(
            f"the demo session finished but produced no package at {package}. "
            "A demo run's whole output is that folder."
        )

    files = sorted(p for p in package.rglob("*") if p.is_file())
    videos = [p for p in files if p.suffix.lower() in _VIDEO_SUFFIXES]
    if not videos:
        raise RuntimeError(
            f"no video in {package} (found: {', '.join(p.name for p in files) or 'nothing'}). "
            "The recording is the point; a folder of code is not a demo."
        )

    deliverable = [p for p in videos if p.suffix.lower() == _session_config.DELIVERABLE_SUFFIX]
    if not deliverable:
        found = ", ".join(sorted(p.name for p in videos))
        raise RuntimeError(
            f"the package has no {_session_config.DELIVERABLE_SUFFIX} video (found: {found}). The final "
            f"video "
            "has to play for someone who did not build it -- WebM does not import into Keynote "
            "or PowerPoint and QuickTime will not open it. Convert the take you have:\n"
            "    ffmpeg -i <in>.webm -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p "
            "-c:a aac -b:a 160k -movflags +faststart <out>.mp4\n"
            "Keep the WebM screen capture under assets/ as a source; it is the *deliverable* "
            "that has to be portable."
        )
    video = max(deliverable, key=lambda p: p.stat().st_size)
    # A container with headers but no frames is a few KB, and that is what a
    # recording that was started and stopped with nothing in between looks like.
    if video.stat().st_size < 50_000:
        raise RuntimeError(
            f"{video.name} is only {video.stat().st_size} bytes, which is an empty recording "
            "rather than a demo. Re-record it."
        )

    _check_audio(video)
    _check_no_scaffolding(package, files)

    if not any(p.name.lower() == "readme.md" for p in files):
        raise RuntimeError(
            f"no README.md in {package}. Someone receiving this folder with no context needs "
            "to be told what it is and how to run it."
        )

    code = [p for p in files if p.suffix in (".py", ".ts", ".js", ".go", ".rs", ".java", ".sh", ".ipynb")]
    if not code:
        raise RuntimeError(
            f"no code in {package}. The package is meant to be the shareable subset of the "
            "work, not the video alone."
        )

    _check_demo_assets(package, files)
    return video, files


#: Markers that give away how a package was produced. A prospect-facing demo
#: should stand on its own; these also tend to come with internal detail
#: (briefs, run ids, task names) that nobody outside meant to publish.
_SCAFFOLDING_MARKERS = (
    "claude-agent",
    "agent-factory",
    "claude-demo",
    "flyte_agent_factory",
    "BRIEF.md",
    "CLAUDE.md",
    ".claude-flyte",
    "flyte-note",
    "ask-human",
)


#: Text files worth reading for those markers. Binaries and images are skipped.
_TEXT_SUFFIXES = (".md", ".html", ".css", ".txt", ".json", ".py", ".sh", ".yaml", ".yml", ".jsonl")


def _check_no_scaffolding(package: Path, files: list[Path]) -> None:
    """Keep the agent's own machinery out of a package meant for outsiders.

    A demo should stand on its own. Leaving `BRIEF.md`, an `agent-factory` task
    name or an `ask-human` call in the shipped files tells the recipient how it
    was made -- and usually drags internal detail along with it. Checked on the
    text files only; the video is the session's own responsibility and the
    memory tells it what not to film.
    """
    found: list[str] = []
    for path in files:
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for marker in _SCAFFOLDING_MARKERS:
            if marker in text:
                found.append(f"{path.relative_to(package).as_posix()}: {marker}")
    if found:
        raise RuntimeError(
            "the package names the machinery that built it, which a recipient outside the "
            f"company should not see: {'; '.join(sorted(set(found))[:10])}. Remove the "
            "references, or if the demo is deliberately about the agent, say so in the brief."
        )


def _check_audio(video: Path) -> None:
    """Fail a video whose audio is present but too quiet to hear.

    Checking that an audio *stream* exists proves nothing: a stream of near
    silence passes that test perfectly. A demo shipped with narration at -25.5
    LUFS -- roughly 10 dB under normal dialogue -- and the first question it got
    was whether it was supposed to have sound. Non-fatal when ffprobe or ffmpeg
    are unavailable, because the package is still the deliverable.
    """
    if not shutil.which("ffmpeg"):
        return
    probe = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        # ffmpeg exits non-zero writing to the null muxer in some builds; the
        # measurement is on stderr either way, so the exit code is not the
        # signal here.
        check=False,
    )
    # volumedetect prints at info level, which is why `-v error` shows nothing.
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", probe.stderr)
    if match is None:
        # No audio stream at all, or ffmpeg could not tell us. Silence is a
        # legitimate choice when there is no voice model, so this is not fatal.
        return
    mean = float(match.group(1))
    if mean < -24.0:
        raise RuntimeError(
            f"{video.name}'s audio averages {mean:.1f} dB, which is too quiet to hear on "
            "laptop speakers -- synthesised narration comes out around -25 LUFS and needs "
            "normalising. Re-render the last step with "
            "`-af loudnorm=I=-16:TP=-1.5:LRA=11` and check with "
            "`ffmpeg -i out.mp4 -af volumedetect -f null -` (expect about -18 dB)."
        )


def _check_demo_assets(package: Path, files: list[Path]) -> None:
    """Fail unless the video's ingredients were kept alongside it.

    A finished video alone cannot be edited: changing the music or one slide's
    wording would mean recording the whole demo again. The manifest plus the
    per-section sources are what make a later "swap this one element"
    instruction cheap, so they are part of the deliverable rather than
    scaffolding to clean up.
    """
    manifest_path = package / _session_config.DEMO_ASSETS_SUBDIR / _session_config.DEMO_MANIFEST
    if not manifest_path.is_file():
        raise RuntimeError(
            f"no {_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST} in {package}. The "
            f"pieces the video was "
            "composed from have to be kept, or changing the music or one slide means "
            "re-recording everything."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"{_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST} is not UTF-8: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST} is not valid JSON: {e}"
        ) from e
    if not isinstance(manifest, dict):
        raise TypeError(
            f"{_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST} is a JSON "
            f"{type(manifest).__name__}, not an object "
            "with `audience`, `subject` and `sections`."
        )

    sections = manifest.get("sections")
    if not isinstance(sections, list) or not sections:
        raise RuntimeError(
            f"{_session_config.DEMO_MANIFEST} has no `sections` list. It should describe the composition in "
            "order, so somebody can find the one piece they want changed."
        )

    # Who it is for and what it is about are decisions, not metadata: a demo
    # made without them ends up a feature tour aimed at nobody in particular.
    for key, what in (
        ("audience", "the role watching and the decision they are making"),
        ("subject", "the capability being demonstrated, as distinct from the example used"),
    ):
        value = manifest.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"{_session_config.DEMO_MANIFEST} has no `{key}`. State {what}, in one line, before the "
                "sections -- it is what the video is edited against."
            )

    known = {p.relative_to(package).as_posix() for p in files}
    missing: list[str] = []
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            raise TypeError(f"{_session_config.DEMO_MANIFEST} section {index} is not an object")
        for key in ("slide", "voiceover", "music"):
            ref = section.get(key)
            # Absent is fine -- not every section has narration or music. A
            # reference to a file that is not there is not fine.
            if isinstance(ref, str) and ref and ref not in known:
                missing.append(f"section {index} {key}: {ref}")
    if missing:
        raise RuntimeError(
            f"{_session_config.DEMO_MANIFEST} references files that are not in the package: "
            f"{'; '.join(missing)}"
        )


async def _fetch_voice_model(work: Path, timeline: _timeline._Timeline) -> Path | None:
    """Download the latest `voice-model` artifact for the session to narrate with.

    Done by the task rather than the session because resolving an artifact
    needs a control-plane credential and the session is deliberately run
    without one -- the MCP server echoes back the code it is asked to run, so a
    key in the session's reach ends up in the run log. Handing over a directory
    costs nothing and keeps that property.

    Returns the local path, or None if no model has been published yet, in
    which case the demo is built with music only.
    """
    from flyte import remote

    try:
        artifact = remote.Artifact.get(_VOICE_MODEL_ARTIFACT)
    except REMOTE_ERRORS as e:  # narration is optional
        timeline.row("ℹ️", f"no {_VOICE_MODEL_ARTIFACT} artifact ({type(e).__name__}); music only")
        return None
    if artifact is None:
        # The stub types `Artifact.get` as non-Optional; the guard stays in case a missing artifact is None.
        timeline.row("ℹ️", f"no {_VOICE_MODEL_ARTIFACT} artifact published yet; music only")  # type: ignore[unreachable]  # `Artifact.get` is typed as never missing
        return None

    target = work / ".voice-model"
    try:
        model = await artifact.to_python(Dir)
        await model.download(str(target))
    except REMOTE_ERRORS as e:  # narration is optional
        timeline.row("⚠️", f"could not download {_VOICE_MODEL_ARTIFACT}: {type(e).__name__}: {e}")
        return None

    timeline.row("🎙️", f"{_VOICE_MODEL_ARTIFACT} {artifact.version} at {target.name}")
    return target
