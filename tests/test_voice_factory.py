"""The voice-model factory's contract with the rest of the system.

Its value is entirely in the interface: a new audio artifact produces a new
model artifact, and consumers ask for the model by name. If any of that drifts,
demos silently narrate with a stale voice or none at all.
"""

from __future__ import annotations

import inspect
from typing import cast

import flyte

from flyte_agent_factory import _demo_checks, voice


def test_it_triggers_on_the_audio_artifact():
    """This is the automation: no one should have to launch training by hand."""
    assert cast(flyte.OnArtifact, voice.retrain.automation).name == voice.AUDIO_ARTIFACT


def test_it_trains_on_the_version_that_fired_rather_than_latest():
    """Re-resolving "latest" could train on different audio than the one published.

    `TriggeredArtifact` binds the exact version that fired, which matters when
    two versions land close together.
    """
    assert voice.retrain.inputs is not None
    assert voice.retrain.inputs["audio"] is flyte.TriggeredArtifact


def test_a_gpu_forces_on_demand():
    """Spot has no GPU nodepools on this cluster, verified twice.

    `interruptible=True` together with a GPU is rejected at admission, so this
    pairing is load-bearing rather than a preference.
    """
    assert voice.env.resources is not None
    assert voice.env.resources.gpu == 1
    assert voice.env.interruptible is False


def test_it_names_no_gpu_device_class():
    """Naming a device narrows placement for no benefit.

    An earlier run had a probe rejected for asking for a class that was not
    free; letting the scheduler choose avoids that.
    """
    assert voice.env.resources is not None
    assert "nvidia" not in str(voice.env.resources.gpu).lower()


def test_the_factory_contains_no_training_code():
    """The method must be able to change without touching the factory.

    Fine-tuning, zero-shot with a chosen reference clip, or a different model
    entirely are all the project repo's business. The factory only calls two
    fixed entrypoints.

    Asserted on the imports rather than on substrings: an earlier version of
    this test searched for "ema" and matched it inside "on-demand".
    """
    import ast

    tree = ast.parse(inspect.getsource(voice))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    ml = {"torch", "torchaudio", "numpy", "scipy", "soundfile", "librosa", "transformers", "f5_tts"}
    assert not (imported & ml), f"the factory imports training libraries: {imported & ml}"

    assert voice.TRAIN_ENTRYPOINT == "voice/train.py"
    assert voice.SYNTHESIZE_ENTRYPOINT == "voice/synthesize.py"


def _collapsed(obj) -> str:
    """Source with adjacent string literals joined and whitespace squeezed.

    Needed because the formatter splits a long message across literals, so a
    phrase that reads contiguously never appears contiguously in the source.
    """
    import re

    # Squeeze whitespace *first*: the literals are separated by a newline and
    # indentation, so the `" "` join only appears once that is collapsed.
    collapsed = re.sub(r"\s+", " ", inspect.getsource(obj))
    return collapsed.replace('" "', "")


def test_it_fails_loudly_when_the_entrypoint_is_absent():
    """Better than publishing an empty model that demos would then narrate with."""
    source = _collapsed(voice.train.func)
    assert "is not in {repo}" in source
    assert "does not contain any training code of its own" in source


def test_it_refuses_to_publish_an_empty_model():
    """A trainer that wrote nothing must not become a `voice-model` version.

    A consumer resolves `voice-model` to the newest version; an empty one would
    break every later demo until someone noticed.
    """
    assert "wrote nothing" in _collapsed(voice.train.func)


def test_the_demo_task_resolves_the_model_by_name_on_the_sessions_behalf():
    """Demos must pick up a newer model without a code change.

    Resolution moved from the session to the task: the session has no
    control-plane credential by design, so it is handed a directory instead of
    a name. The "always the latest" property lives in the task now.
    """
    import inspect

    assert _demo_checks._VOICE_MODEL_ARTIFACT == voice.MODEL_ARTIFACT
    fetch = inspect.getsource(_demo_checks._fetch_voice_model)
    # No version pin: `Artifact.get(name)` resolves to the newest.
    assert "version=" not in fetch


def test_it_installs_into_the_interpreter_that_will_run_the_entrypoint():
    """`--system` is the wrong target in this image.

    The image sets `VIRTUAL_ENV=/opt/venv`, so a `--system` install lands
    somewhere this Python does not look: the install succeeds and the import
    fails. Both the install and the call use `sys.executable`.
    """
    import ast
    import inspect

    # Inspect the actual call arguments. Checking the file text matched the
    # comment that *explains* why `--system` is wrong, which is not a bug.
    tree = ast.parse(inspect.getsource(voice.train.func).lstrip())
    installs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run"
        and any(isinstance(a, ast.Constant) and a.value == "pip" for a in node.args)
    ]
    assert installs, "no pip install call found"
    flags = [a.value for a in installs[0].args if isinstance(a, ast.Constant)]
    assert "--system" not in flags
    assert "--python" in flags


def test_the_factory_does_not_import_the_agent():
    """Flyte bundles from the import graph it can see.

    The factory's first run died with `No module named 'flyte_agent_factory.agent'`
    because `_clone` was imported inside the function, so the bundler never
    shipped it. Making that import module-level would have fixed the bundle and
    pulled the browser, the state machinery and the bridge in with it -- hence
    `_git` as a module of its own.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(voice))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "flyte_agent_factory.agent" not in imported
    assert "flyte_agent_factory._git" in imported


def test_the_factorys_imports_are_all_module_level():
    """A function-level import is invisible to the bundler.

    Anything the task body needs at runtime has to be importable from what was
    shipped, so first-party imports belong at the top of the file where the
    bundler can see them.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(voice.train.func).lstrip())
    inner = [
        alias.name or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ] + [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    first_party = [name for name in inner if name.startswith("flyte_agent_factory")]
    assert not first_party, f"first-party imports inside the task body: {first_party}"


def test_the_image_carries_the_voice_dependencies():
    """Installing them at runtime failed, and would be slow even when it works.

    `openai-whisper` ships no wheel; its `setup.py` imports `pkg_resources`,
    and an isolated build fetches setuptools>=81, which removed it. The
    project run's own fix was pinning `setuptools<81`, so the image uses the
    package set it had working rather than a set I guessed at.
    """
    from flyte_agent_factory._images import VOICE_PACKAGES

    assert "setuptools<81" in VOICE_PACKAGES, "the pin is the fix, not an accident"
    for needed in ("f5-tts", "resemblyzer", "openai-whisper", "silero-vad", "scikit-learn"):
        assert needed in VOICE_PACKAGES, f"{needed} is reached via ingest.py"


def test_the_demo_image_carries_the_same_voice_stack():
    """Narration runs in the demo pod, so it needs what synthesize.py imports.

    A demo run finished having produced no package at all: the brief asked for
    narration, `synthesize.py` needs f5-tts and torch, and the image had
    neither. A session cannot usefully install 3GB of wheels mid-recording.
    One shared tuple so the factory and the demo cannot drift.
    """
    from flyte_agent_factory import agent
    from flyte_agent_factory._images import VOICE_PACKAGES

    assert agent.VOICE_PACKAGES is VOICE_PACKAGES  # type: ignore[attr-defined]  # the import is the point, not an export
    assert voice.VOICE_PACKAGES is VOICE_PACKAGES  # type: ignore[attr-defined]  # the import is the point, not an export


def test_the_runtime_top_up_is_not_fatal():
    """The image is normally sufficient; a failed top-up must not lose the run.

    This runs on a GPU, after downloading the audio -- throwing away that work
    because a pip resolution hiccuped would be the wrong trade.
    """
    source = _collapsed(voice.train.func)
    assert "requirements top-up failed, continuing on the image" in source
    # And the build-isolation flag whisper needs is present.
    assert "--no-build-isolation" in source
