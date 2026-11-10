"""The demo package contract.

Every failure the validator catches is silent downstream: a run that exits 0
having recorded nothing looks exactly like one that worked, and the person who
finds out is the one about to send it to a customer.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from flyte_agent_factory import (
    _demo_checks,
    _instructions,
    _review,
    _session_config,
    _session_setup,
    _supervise,
    _supervise_support,
    _timeline,
)
from flyte_agent_factory._demo_checks import _check_demo_package
from tests._wait import wait_for


def _package(
    tmp_path,
    *,
    video_bytes: int | None = 200_000,
    readme=True,
    code=True,
    assets=True,
    manifest=None,
    extra=(),
):
    package = tmp_path / _session_config.DEMO_SUBDIR / "polynomial"
    package.mkdir(parents=True)
    if video_bytes is not None:
        (package / "polynomial.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * (video_bytes - 12))
    if readme:
        (package / "README.md").write_text("# Polynomial demo\n")
    if code:
        (package / "train.py").write_text("print('hi')\n")
    if assets:
        asset_dir = package / _session_config.DEMO_ASSETS_SUBDIR
        (asset_dir / "slides").mkdir(parents=True)
        (asset_dir / "slides" / "01-problem.html").write_text("<h1>The problem</h1>")
        (asset_dir / "audio").mkdir(parents=True)
        (asset_dir / "audio" / "bed.wav").write_bytes(b"\0" * 64)
        (asset_dir / _session_config.DEMO_MANIFEST).write_text(
            json.dumps(
                manifest
                if manifest is not None
                else {
                    "audience": "platform lead deciding whether to trial this",
                    "subject": "fitting a model on Flyte",
                    "sections": [
                        {
                            "title": "The problem",
                            "slide": "assets/slides/01-problem.html",
                            "music": "assets/audio/bed.wav",
                        }
                    ],
                }
            )
        )
    for name in extra:
        (package / name).write_text("x\n")
    return package


def test_a_good_package_passes(tmp_path):
    package = _package(tmp_path)
    video, files = _check_demo_package(package)
    assert video.name == "polynomial.mp4"
    assert {"polynomial.mp4", "README.md", "train.py"} <= {p.name for p in files}


def test_a_missing_package_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="produced no package"):
        _check_demo_package(tmp_path / _session_config.DEMO_SUBDIR / "nope")


def test_code_without_a_video_is_not_a_demo(tmp_path):
    package = _package(tmp_path, video_bytes=None)
    with pytest.raises(RuntimeError, match="no video"):
        _check_demo_package(package)


def test_an_empty_recording_is_rejected(tmp_path):
    """Start-then-stop with nothing in between yields a few KB of container.

    This is the failure that most looks like success: the file exists, the tool
    call returned a path, and nothing errored.
    """
    package = _package(tmp_path, video_bytes=3_000)
    with pytest.raises(RuntimeError, match="empty recording"):
        _check_demo_package(package)


def test_a_package_nobody_can_use_is_rejected(tmp_path):
    package = _package(tmp_path, readme=False)
    with pytest.raises(RuntimeError, match="no README.md"):
        _check_demo_package(package)


def test_a_video_with_no_code_is_rejected(tmp_path):
    """The package is the shareable subset of the work, not the video alone."""
    package = _package(tmp_path, code=False)
    with pytest.raises(RuntimeError, match="no code"):
        _check_demo_package(package)


def test_the_largest_video_is_the_take(tmp_path):
    """A re-record leaves the earlier attempt behind; the real take is bigger."""
    package = _package(tmp_path, video_bytes=120_000)
    (package / "first-attempt.mp4").write_bytes(b"\0" * 60_000)
    video, _ = _check_demo_package(package)
    assert video.name == "polynomial.mp4"


def test_webm_alone_is_not_a_deliverable(tmp_path):
    """WebM is what the recorder produces, not what anyone can play.

    It does not import into Keynote or PowerPoint, QuickTime will not open it,
    and mail clients often fail to preview it. The package exists to be sent to
    someone, so the final video has to be MP4.
    """
    package = _package(tmp_path, video_bytes=None)
    (package / "polynomial.webm").write_bytes(b"\0" * 200_000)
    with pytest.raises(RuntimeError, match="no .mp4 video"):
        _check_demo_package(package)


def test_the_raw_webm_capture_is_allowed_as_a_source(tmp_path):
    """Only the deliverable has to be portable; assets/screen.webm is a source."""
    package = _package(tmp_path)
    (package / _session_config.DEMO_ASSETS_SUBDIR / "screen.webm").write_bytes(b"\0" * 300_000)
    video, _ = _check_demo_package(package)
    assert video.name == "polynomial.mp4", "the mp4 is the deliverable, not the bigger webm"


def test_memory_states_the_contract_and_how_to_get_the_file_out(tmp_path):
    """Two things cost time before: fetching the video, and trimming the code."""
    memory = _instructions._recording_note()

    # The folder is named by the brief, after the work -- never after the run.
    # A session lost a turn asking whether to use the brief's name or the run's.
    assert f"a folder under `{_session_config.DEMO_SUBDIR}/`" in memory
    assert "The brief names it" in memory
    assert "Never name it after this run or this project" in memory
    assert "README.md" in memory
    assert _session_config.DELIVERABLE_SUFFIX in memory

    # The recording does not arrive in the workspace by itself.
    assert "/output/" in memory
    assert "text/plain" in memory

    # Curation is the work, not an afterthought.
    assert "not the repository" in memory

    # In-pod means localhost, which is the better path than a data: URL.
    assert "localhost" in memory

    # Console auth is covered by its own tests below; it depends on whether a
    # credential exists in the pod.


def test_there_is_exactly_one_clone_and_it_installs_the_deploy_key():
    """A clone without the key dies on `Host key verification failed`.

    The key is installed per process, not per clone, so every entry point that
    clones needs it -- and `demo` did not, which is how its first run failed;
    `subagent` had the same bug latently, never having been given a `repo`.
    Three copies of the clone block was the actual defect, so the invariant is
    structural: one helper, in `_git`, that installs the key before shelling
    out.

    `_git` is a module of its own so the voice factory can clone without
    importing the agent. Flyte bundles from the import graph it can see, so the
    factory's first run failed with `No module named 'flyte_agent_factory.agent'`
    on a function-level import -- and a module-level one would have dragged the
    browser, the state machinery and the bridge into its bundle.
    """
    import inspect
    import re

    from flyte_agent_factory import _git, agent, voice

    for module in (agent, voice):
        sites = re.findall(r'"git",\s*"clone"', inspect.getsource(module))
        assert not sites, (
            f"{module.__name__} shells out to `git clone` directly; it should call `_git.clone`. "
            "Duplicating it is how the deploy key got forgotten."
        )

    helper = inspect.getsource(_git.clone)
    assert "install_deploy_key(repo)" in helper
    assert helper.index("install_deploy_key(repo)") < helper.index('"clone"'), (
        "the key has to be installed before the clone runs, not after"
    )
    # And the alias the agent's call sites use still points at it.
    assert _session_setup._clone is _git.clone


def test_memory_tells_it_to_stop_rather_than_package_without_a_video():
    """The first demo run's failure was a silent capability gap.

    `claude -p` does not load a project `.mcp.json`, so the browser tools were
    absent. The session did not report that -- it curated a good package and
    omitted the video, which only the validator caught. The session should say
    so itself rather than work around it.
    """
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))

    assert "browser_start_video" in memory
    assert "do not build the package" in memory.lower()


def test_the_session_installs_console_auth_from_a_file_never_inline():
    """The key must not reach the session's environment or the echoed code.

    The supervisor used to install the header itself, but the in-pod browser is
    `--isolated` now -- each MCP client gets its own context, so a route handler
    added by the supervisor never reaches the session. So the session installs
    it, reading the key from a file: the MCP server echoes back the code it
    ran, and a key pasted inline landed in a run log in full and had to be
    rotated.
    """
    import pathlib

    token = pathlib.Path("/tmp/demo-x/.console-token")
    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"), console_token=token)

    assert str(token) in memory
    assert "readFileSync" in memory
    assert "never paste it into the snippet" in memory
    # Host-scoped, because a blanket header renders the console blank.
    assert "page.context().route(" in memory
    assert "setExtraHTTPHeaders" in memory and "rather than" in memory


def test_without_a_credential_the_session_is_told_to_skip_the_console():
    """Filming a sign-in page is worse than leaving the section out."""
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"), console_token=None)

    assert "No console credential is available" in memory
    assert "Leave that section out" in memory
    assert "readFileSync" not in memory


def test_memory_says_the_signin_redirect_is_expected():
    """A demo run misread the login redirect as the host being unreachable.

    It then reported that the pod could not reach the console and shipped a
    take with a placeholder chapter. A probe from a task pod contradicts it:
    anonymous navigation redirects to the sign-in page, and with the pod's key
    attached the console renders (`Runs | Union.ai`). The redirect is how an
    unauthenticated request is supposed to look.
    """
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))

    assert "expected, not a sign the console is unreachable" in memory
    # And console errors are not a failure signal on that page.
    assert "84 console errors" in memory


def test_the_report_embeds_a_small_video_inline(tmp_path):
    """Inline rather than a presigned S3 URL, which would expire.

    The report is one HTML string -- there is no way to attach a file beside
    it, and a relative URL would not resolve to a sibling object in storage. A
    presigned link plays today and is a broken player next week, which is worse
    than a large file for something anyone revisits.
    """
    from flyte_agent_factory._demo_checks import _demo_report

    package = _package(tmp_path, video_bytes=200_000)
    video = package / "polynomial.mp4"
    files = sorted(p for p in package.rglob("*") if p.is_file())

    report = _demo_report(name="polynomial", video=video, files=files, summary="done", pushed="abc1234")

    assert "<video controls" in report
    assert "data:video/mp4;base64," in report
    # The other files are listed, but the video is not listed as well as played.
    assert "README.md" in report
    assert report.count("polynomial.mp4") <= 1
    # And it says where to find the shareable copy.
    assert "abc1234" in report
    assert "demo/polynomial/" in report


def test_the_report_links_rather_than_embeds_an_oversized_video(tmp_path):
    """Base64 inflates by ~4/3 and the browser pulls the whole report first.

    A 50MB inline video makes the report unusable rather than making the video
    available, so past a cap it is described instead.
    """

    package = _package(tmp_path, video_bytes=_demo_checks._REPORT_VIDEO_MAX_BYTES + 1_000_000)
    video = package / "polynomial.mp4"
    files = sorted(p for p in package.rglob("*") if p.is_file())

    report = _demo_checks._demo_report(name="p", video=video, files=files, summary="s", pushed=None)

    assert "data:video/webm;base64," not in report
    assert "too large to embed" in report
    # And it is honest that nothing was pushed.
    assert "Not pushed to git" in report


def test_the_report_says_when_there_is_no_repo_to_push_to(tmp_path):
    from flyte_agent_factory._demo_checks import _demo_report

    package = _package(tmp_path)
    video = package / "polynomial.mp4"
    files = sorted(p for p in package.rglob("*") if p.is_file())

    report = _demo_report(name="p", video=video, files=files, summary="s", pushed=None)
    assert "Not pushed to git" in report


def test_a_video_without_its_ingredients_is_rejected(tmp_path):
    """A finished video alone cannot be edited.

    The whole point of keeping the pieces is that "change the music" or
    "reword slide 3" re-renders instead of re-recording, so the manifest and
    the sources are part of the deliverable.
    """
    package = _package(tmp_path, assets=False)
    with pytest.raises(
        RuntimeError, match=f"{_session_config.DEMO_ASSETS_SUBDIR}/{_session_config.DEMO_MANIFEST}"
    ):
        _check_demo_package(package)


def test_a_manifest_with_no_sections_is_rejected(tmp_path):
    package = _package(tmp_path, manifest={"sections": []})
    with pytest.raises(RuntimeError, match="no `sections` list"):
        _check_demo_package(package)


def test_a_manifest_pointing_at_a_missing_file_is_rejected(tmp_path):
    """The failure this prevents is silent: a manifest that looks complete.

    Someone asked to change the music would follow the reference and find
    nothing there.
    """
    package = _package(
        tmp_path,
        manifest={
            "audience": "a platform lead",
            "subject": "fitting a model on Flyte",
            "sections": [{"title": "x", "slide": "assets/slides/nope.html"}],
        },
    )
    with pytest.raises(RuntimeError, match="references files that are not in the package"):
        _check_demo_package(package)


def test_sections_need_not_have_narration_or_music(tmp_path):
    """Absent is fine; dangling is not. A silent title card is legitimate."""
    package = _package(
        tmp_path,
        manifest={
            "audience": "a platform lead",
            "subject": "fitting a model on Flyte",
            "sections": [{"title": "Just a slide"}],
        },
    )
    video, _ = _check_demo_package(package)
    assert video.name == "polynomial.mp4"


def test_the_in_pod_browser_is_isolated_so_the_supervisor_cannot_take_the_seat():
    """Playwright allows one client per persistent profile, and the task is one.

    The supervisor's own pre-flight tool listing held the profile, so the
    session it had just started got "Browser is already in use" on every call
    and could not record anything. Isolation is what lets both exist.
    """
    import inspect
    import pathlib

    from flyte_agent_factory._local_browser import LocalBrowser

    browser = LocalBrowser(profile_dir=pathlib.Path("/tmp/p"), output_dir=pathlib.Path("/tmp/o"))
    assert browser._isolated is True, "the default must not contend for a profile"

    source = inspect.getsource(LocalBrowser.start)
    assert "--isolated" in source
    assert "--user-data-dir" in source, "the persistent mode should still be reachable"


def test_the_task_fetches_the_voice_model_so_the_session_needs_no_credential():
    """Two fixes, because the first one was not enough.

    A demo could not resolve `voice-model`: `attach()` raised with no
    `.flyte/run_context.json`. Writing the context fixed child actions but not
    artifacts -- resolving one needs a control-plane credential, and
    `_without_api_key` had deliberately removed it. So the task, which has the
    key, downloads the model and hands over a directory. The session keeps no
    credential, which is the property that stops the key reaching the run log.
    """
    import inspect

    from flyte_agent_factory import agent

    source = inspect.getsource(agent.project.func)
    assert "write_context(_session_config.WORKSPACE)" in source, "child actions still need the context"
    assert "_fetch_voice_model(_session_config.WORKSPACE, timeline)" in source

    fetch = inspect.getsource(_demo_checks._fetch_voice_model)
    assert "Artifact.get(_VOICE_MODEL_ARTIFACT)" in fetch
    assert "music only" in fetch, "a missing model must not fail the run"


def test_narration_note_hands_over_a_path_not_an_artifact_name():
    """The session cannot resolve artifacts, so telling it a name is useless."""
    import pathlib

    model = pathlib.Path("/tmp/demo-x/.voice-model")
    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"), voice_model=model)

    assert str(model) in memory
    assert "you have no credential to do so with" in memory
    assert "synthesize.py" in memory
    # And with no model, music only rather than a substitute voice.
    silent = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"), voice_model=None)
    assert "No voice model is available" in silent
    assert "worse than a silent one" in silent


def test_memory_forbids_a_synthesised_music_bed():
    """A generated tone is a hum, and a hum is worse than silence."""
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))
    assert "Do not synthesise a music bed" in memory
    assert "worse than silence" in memory


@pytest.mark.asyncio
async def test_the_report_is_flushed_while_a_long_task_runs():
    """Otherwise the report says "no report yet" for the whole run.

    Any `flyte-note` the session posted was invisible until the task ended,
    which is the opposite of what the notes are for.
    """
    import asyncio

    flushed: list[bool] = []

    class _Recording(_timeline._Timeline):
        async def flush(self, force: bool = False) -> None:
            flushed.append(force)

    stop = asyncio.Event()
    loop = asyncio.create_task(_supervise_support._flush_loop(_Recording(), stop, every=0.01))
    await wait_for(lambda: len(flushed) > 1)
    stop.set()
    await asyncio.wait_for(loop, timeout=30)

    assert len(flushed) > 1, "the report was not refreshed while the task ran"


@pytest.mark.asyncio
async def test_a_failing_flush_does_not_kill_the_loop():
    """A transient storage error must not blind the report for the rest of the run."""
    import asyncio

    calls: list[int] = []

    class _Broken(_timeline._Timeline):
        async def flush(self, force: bool = False) -> None:
            calls.append(1)
            raise RuntimeError("object storage hiccup")

    stop = asyncio.Event()
    loop = asyncio.create_task(_supervise_support._flush_loop(_Broken(), stop, every=0.01))
    # Wait for a second attempt, not a fixed 50ms: each failure now prints its traceback, and under a loaded
    # parallel run 50ms did not always fit two.
    await wait_for(lambda: len(calls) > 1)
    stop.set()
    await asyncio.wait_for(loop, timeout=30)

    assert len(calls) > 1, "the loop gave up after the first failure"


def test_memory_gives_the_conversion_command_with_the_flags_that_matter():
    """Two flags are load-bearing and easy to omit.

    Without `-pix_fmt yuv420p` some players show nothing at all; without
    `-movflags +faststart` the file will not begin playing until it has fully
    downloaded, which is exactly wrong for something emailed or dropped in a
    channel.
    """
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))

    assert "libx264" in memory
    assert "-pix_fmt yuv420p" in memory
    assert "-movflags +faststart" in memory
    assert "not optional decoration" in memory


def test_a_video_too_quiet_to_hear_is_rejected(tmp_path, monkeypatch):
    """An audio stream of near-silence passes every structural check.

    A demo shipped with narration at -25.5 LUFS -- about 10 dB under normal
    dialogue -- and the first question it got was whether it was supposed to
    have sound. Confirming an AAC stream exists proves nothing about whether
    anyone can hear it.
    """

    class _Probe:
        stderr = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -26.7 dB\n"

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Probe())

    with pytest.raises(RuntimeError, match="too quiet to hear"):
        _demo_checks._check_audio(tmp_path / "demo.mp4")


def test_a_normalised_video_passes(tmp_path, monkeypatch):

    class _Probe:
        stderr = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -17.7 dB\n"

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Probe())

    _demo_checks._check_audio(tmp_path / "demo.mp4")  # must not raise


def test_a_silent_video_is_allowed(tmp_path, monkeypatch):
    """Silence is a legitimate choice when there is no voice model."""

    class _Probe:
        stderr = "no audio stream\n"

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Probe())

    _demo_checks._check_audio(tmp_path / "demo.mp4")  # must not raise


def test_memory_tells_it_to_normalise_and_verify_loudness():
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))
    assert "loudnorm=I=-16" in memory
    assert "volumedetect" in memory
    # The trap that hid it from me: volumedetect is silent under -v error.
    assert "says nothing under `-v error`" in memory


def test_a_package_naming_the_agent_is_rejected(tmp_path):
    """A prospect-facing demo should stand on its own.

    Leaving `BRIEF.md`, an `agent-factory` task name or an `ask-human` call in the
    shipped files tells the recipient how it was made, and usually drags
    internal detail along with it.
    """
    package = _package(tmp_path)
    (package / "README.md").write_text("# Demo\n\nRun the pipeline. See BRIEF.md for the original scope.\n")
    with pytest.raises(RuntimeError, match="names the machinery that built it"):
        _check_demo_package(package)


def test_scaffolding_in_code_is_caught_too(tmp_path):
    package = _package(tmp_path)
    (package / "train.py").write_text("import flyte_agent_factory\nprint('hi')\n")
    with pytest.raises(RuntimeError, match="flyte_agent_factory"):
        _check_demo_package(package)


def test_binaries_are_not_scanned(tmp_path):
    """The video legitimately contains bytes that could match anything."""
    package = _package(tmp_path)
    # Large enough to clear the "empty recording" floor, and full of bytes that
    # would trip the scaffolding scan if binaries were read as text.
    (package / "polynomial.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"CLAUDE.md claude-agent " * 10_000)
    video, _ = _check_demo_package(package)
    assert video.name == "polynomial.mp4"


def test_a_manifest_without_an_audience_is_rejected(tmp_path):
    package = _package(
        tmp_path,
        manifest={"subject": "the gateway", "sections": [{"title": "One"}]},
    )
    with pytest.raises(RuntimeError, match="no `audience`"):
        _check_demo_package(package)


def test_a_manifest_without_a_subject_is_rejected(tmp_path):
    package = _package(
        tmp_path,
        manifest={"audience": "a platform lead", "sections": [{"title": "One"}]},
    )
    with pytest.raises(RuntimeError, match="no `subject`"):
        _check_demo_package(package)


def test_a_blank_audience_is_rejected(tmp_path):
    package = _package(
        tmp_path,
        manifest={"audience": "  ", "subject": "the gateway", "sections": [{"title": "One"}]},
    )
    with pytest.raises(RuntimeError, match="no `audience`"):
        _check_demo_package(package)


def test_the_filmed_run_must_be_a_separate_top_level_run():
    """Drilling into child actions was not enough, and shipped twice broken.

    A child action still renders under its parent's header, so the console
    showed `agent-factory.project`, a day-long supervisor and an Abort button.
    The only clean view is a run the session created for the purpose, which it
    gets by *not* calling `attach()`.
    """
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))

    assert "never this one" in memory
    assert "not a child action of it" in memory
    assert "Run it without `attach()`" in memory
    assert "no `claude-*` task names" in memory
    assert "leave the section out rather than filming the scaffolding" in memory


def test_the_filmed_environment_is_named_for_the_work():
    """The TaskEnvironment name is the largest text on the console page."""
    import pathlib

    memory = _instructions._recording_note(name="x", package=pathlib.Path("a/b/demo/x"))
    assert "Name its `TaskEnvironment` for the work" in memory


def test_project_memory_carries_the_recording_rules_when_a_browser_exists(monkeypatch):
    """The demo task is gone; these rules have to reach the project session."""
    # With the context guard (on by default) they reach it only for a demo brief: tests/test_context_guard.py.
    monkeypatch.setenv("AGENT_FACTORY_CONTEXT_GUARD", "0")

    with_browser = _instructions._project_memory(True)
    assert "Recording a demo of this work" in with_browser
    assert "never this one" in with_browser

    # A project with no browser cannot record, so the section is noise.
    assert "Recording a demo of this work" not in _instructions._project_memory(False)


def test_the_session_is_told_it_cannot_grade_itself():

    memory = _instructions._project_memory(False)
    assert "Finishing is a gate, not a decision" in memory
    assert "ACCEPTANCE.md" in memory
    assert "Never weaken a test" in memory
    assert "PushNotification" in memory


def test_the_loop_is_capped_and_escalates():
    import inspect

    # 6 since the planner: hard briefs were still converging at round 3 (the operations log). The
    # cap stays a hard backstop; before it, the planner's "not converging" escalates.
    assert _session_config._QA_MAX_ROUNDS == 8
    source = inspect.getsource(_supervise._pump)
    assert "escalating" in source
    # The cap is `_max_rounds()`: 8, plus at most `_MAX_GRANTED_ROUNDS` extra rounds that only a
    # direction (`/review-again`) can grant -- never the session.
    assert "qa_round >= _session_config._max_rounds()" in source
    # The longer qualified name made `ruff format` wrap the condition; compare it unwrapped.
    flat = " ".join(source.split())
    assert (
        "converging is False or (converging is None and _review._stalled(_session_config._round_scores))"
        in flat
    )
    assert _session_config._max_rounds() == _session_config._QA_MAX_ROUNDS
    assert _session_config._MAX_GRANTED_ROUNDS <= 2


async def _git(tmp_path, *argv):
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "git", *argv, cwd=str(tmp_path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()


def test_each_judge_covers_one_concern_and_is_told_to_stay_there():
    """One reviewer asked to cover everything writes a little about each and
    lands hard on none. The designs that work separate the roles instead."""
    from flyte_agent_factory._review import _JUDGES, _qa_prompt

    names = [name for name, _ in _JUDGES]
    assert names == ["correctness", "completeness", "craft"]

    for name, instruction in _JUDGES:
        prompt = _qa_prompt(name, instruction, "the brief", "- a criterion", 1)
        assert f"You are the **{name}** reviewer" in prompt
        assert "stay on yours and do not" in prompt
        # Every judge keeps the shared guardrails.
        assert "This is the authority" in prompt
        assert "treat it as a proposal, not a standard" in prompt
        assert "Only the work this brief covers" in prompt
        assert "your output is findings, not a fix" in prompt


def test_the_correctness_judge_watches_for_weakened_tests():
    """Agents under pressure delete failing cases to make a suite pass."""
    from flyte_agent_factory._review import _JUDGES

    correctness = dict(_JUDGES)["correctness"]
    assert "weaken assertions and delete failing cases" in correctness
    assert "compare against git history" in correctness


def test_a_missing_acceptance_file_is_itself_a_finding():
    from flyte_agent_factory._review import _JUDGES, _qa_prompt

    name, instruction = _JUDGES[0]
    assert "They never wrote one" in _qa_prompt(name, instruction, "b", "", 1)


def test_one_rejection_fails_the_round():
    """Any judge can reject: a craft failure is still a failure."""
    import inspect

    source = inspect.getsource(_review._evaluate)
    assert "if not failed:" in source
    assert "return True" in source


def test_a_judge_that_returns_no_verdict_fails():
    """A reviewer that wandered off checked nothing; silence must not pass."""
    import inspect

    source = inspect.getsource(_review._one_judge)
    assert "No verdict is not a pass" in source
    assert "return judge, False" in source


def test_a_broken_judge_does_not_sink_the_round():
    """One reviewer failing to start must not hold a finished project open."""
    import inspect

    source = inspect.getsource(_review._one_judge)
    assert "could not run" in source
    assert "return judge, True" in source


@pytest.mark.asyncio
async def test_only_the_named_package_is_checked(tmp_path):
    """Inference broke the moment two agents shared a repository."""
    from flyte_agent_factory._demo_checks import _demo_package_problem

    mine = tmp_path / _session_config.DEMO_SUBDIR / "mine"
    mine.mkdir(parents=True)
    (mine / "README.md").write_text("# mine\n")
    (tmp_path / _session_config.DEMO_SUBDIR / "theirs").mkdir(parents=True)

    problem = await _demo_package_problem(tmp_path, "mine")
    assert "`demo/mine`" in problem
    assert "theirs" not in problem

    # No package named: not a demo brief, so nothing to gate on.
    assert await _demo_package_problem(tmp_path, "") == ""


@pytest.mark.asyncio
async def test_a_named_package_that_is_missing_is_reported(tmp_path):
    """Silence would read as success for a demo that was never built."""
    from flyte_agent_factory._demo_checks import _demo_package_problem

    (tmp_path / _session_config.DEMO_SUBDIR / "other").mkdir(parents=True)
    problem = await _demo_package_problem(tmp_path, "expected")
    assert "does not exist" in problem
    assert "other" in problem


def test_the_acceptance_criteria_are_pinned_out_of_reach(tmp_path):
    """The agent must not be able to move its own goalposts between rounds."""
    from flyte_agent_factory._contract import _pin_acceptance

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pinned = tmp_path / "outside" / "pinned"
    timeline = _timeline._Timeline()

    criteria = workspace / _session_config._acceptance_rel(_session_config._project_name)
    criteria.parent.mkdir(parents=True, exist_ok=True)
    criteria.write_text("- the real standard\n")
    assert "the real standard" in _pin_acceptance(workspace, pinned, timeline)

    # Rewriting it later changes nothing: the first copy is the standard.
    criteria.write_text("- anything I manage is fine\n")
    again = _pin_acceptance(workspace, pinned, timeline)
    assert "the real standard" in again
    assert "anything I manage" not in again


def test_no_acceptance_file_pins_nothing(tmp_path):
    from flyte_agent_factory._contract import _pin_acceptance

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert _pin_acceptance(workspace, tmp_path / "pinned", _timeline._Timeline()) == ""


def test_the_session_must_itemise_its_criteria_before_claiming_done():
    """Premature completion is the documented failure of long agent runs.

    A list with a verdict per criterion, and what was run to decide it, catches
    the half-built thing nobody mentioned; a feeling that the work is finished
    does not.
    """

    memory = _instructions._project_memory(False)
    assert "go through your criteria one at a time" in memory
    assert "a verdict per line" in memory
    assert "If a criterion is not met, you are not done" in memory


def test_the_session_is_told_not_to_look_for_reasons_to_stop():

    memory = _instructions._project_memory(False)
    assert "Finishing is not the goal" in memory
    assert "that is the moment to keep going instead" in memory
