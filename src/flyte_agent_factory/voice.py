"""The voice-model factory: new audio in, a new model artifact out.

Publish a new version of the `voice-target-audio` artifact and this retrains
and publishes a new `voice-model`. Nothing has to be launched by hand, and a
demo run asks for `voice-model` by name and gets the newest one -- so adding
recordings improves every later demo without touching any code.

    voice-target-audio (artifact)
          │  a new version is published
          ▼
    flyte.Trigger(automation=flyte.OnArtifact("voice-target-audio"))
          │
          ▼
    train()  ──►  voice-model (artifact)
          │
          ▼
    a demo run: Artifact.get("voice-model")

This file deliberately contains **no** training code. The model is developed in
the project repository, by a project run, and this calls it through two fixed
entrypoints:

    voice/train.py       --audio <dir> --out <dir>
    voice/synthesize.py  --model <dir> --text <file> --out <wav>

Keeping the interface here and the implementation there is what lets the
method change -- fine-tuning, zero-shot with a chosen reference clip, a
different model entirely -- without touching the factory or any consumer. If
the honest answer is that zero-shot wins, `train.py` writes out the chosen
reference clip and its config, and the contract is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import flyte
from flyte import remote
from flyte.io import Dir

from flyte_agent_factory import _profile
from flyte_agent_factory._git import clone
from flyte_agent_factory._images import VOICE_PACKAGES, agent_image

#: The artifact this watches, produced by whoever stages recordings.
AUDIO_ARTIFACT = "voice-target-audio"

#: The artifact this publishes, consumed by demo runs.
MODEL_ARTIFACT = "voice-model"

#: Where the entrypoints live in the project repository.
TRAIN_ENTRYPOINT = "voice/train.py"
SYNTHESIZE_ENTRYPOINT = "voice/synthesize.py"
REQUIREMENTS = "voice/requirements.txt"

PROFILE = _profile.require()
DEFAULT_REPO = PROFILE.repo

retrain = flyte.Trigger(
    name="retrain_voice_on_new_audio",
    automation=flyte.OnArtifact(name=AUDIO_ARTIFACT),
    # `TriggeredArtifact` binds the version that fired the trigger, so the run
    # trains on exactly the audio that was published rather than re-resolving
    # "latest" and possibly picking up something else.
    inputs={"audio": flyte.TriggeredArtifact},
)

# A GPU, and therefore on-demand: this cluster has no GPUs in any spot
# nodepool, so `interruptible=True` with a GPU is rejected at admission. No
# device class is named, so the scheduler can use whatever is free.
env = flyte.TaskEnvironment(
    name="voice-factory",
    image=(
        agent_image(browser=False)
        # ffmpeg and libsndfile are what soundfile and the decoders need; the
        # project run found both.
        .with_apt_packages("ffmpeg", "libsndfile1")
        .with_pip_packages(*VOICE_PACKAGES)
    ),
    resources=flyte.Resources(cpu=(2, 8), memory=("8Gi", "32Gi"), gpu=1, disk="50Gi"),
    interruptible=False,
    env_vars={"PYTHONUNBUFFERED": "1", **_profile.pod_env()},
    secrets=[flyte.Secret(key=PROFILE.secrets.deploy_key, as_env_var="GIT_DEPLOY_KEY")],
)


# Flyte accepts trigger lists at runtime; its overload requires a tuple.
@env.task(triggers=[retrain], report=True)  # type: ignore[call-overload, untyped-decorator]
async def train(audio: Dir, repo: str = DEFAULT_REPO, branch: str = "") -> str:
    """Train a voice model from an audio artifact and publish the result.

    Args:
        audio: The recordings to train on. Bound to the artifact version that
            fired the trigger, or passed explicitly for a manual run.
        repo: Repository holding `voice/train.py`.
        branch: Branch to clone.

    Returns:
        A one-line summary naming the artifact version it published.

    """
    import flyte.report

    work = Path("/tmp/voice-factory")
    audio_dir = work / "audio"
    out_dir = work / "model"
    work.mkdir(parents=True, exist_ok=True)

    await clone(repo, branch, work / "repo")
    checkout = work / "repo"

    entrypoint = checkout / TRAIN_ENTRYPOINT
    if not entrypoint.is_file():
        raise RuntimeError(
            f"{TRAIN_ENTRYPOINT} is not in {repo}. The factory calls that script and does not "
            "contain any training code of its own; it has to exist and take "
            "`--audio <dir> --out <dir>`."
        )

    await audio.download(str(audio_dir))

    # Only as a top-up: the image already carries the known-good set, so this
    # exists to pick up something `train.py` has newly started needing without
    # a redeploy. A failure here is reported and tolerated rather than fatal,
    # because the image is usually already sufficient.
    requirements = checkout / REQUIREMENTS
    if requirements.is_file():
        # Into the interpreter that will actually run `train.py`, found via
        # `sys.executable` rather than `--system`. The image sets
        # `VIRTUAL_ENV=/opt/venv`, so "system" is not where this Python looks,
        # and the install would succeed while the import failed. `uv` itself is
        # at /usr/bin/uv, copied in by the image builder.
        try:
            await _run(
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                # The env's own setuptools<81 is what `openai-whisper` needs at
                # build time; an isolated build would fetch a newer one.
                "--no-build-isolation",
                "-r",
                str(requirements),
                cwd=checkout,
            )
        except RuntimeError as e:
            print(f"requirements top-up failed, continuing on the image: {e}", flush=True)

    await _run(
        sys.executable,
        TRAIN_ENTRYPOINT,
        "--audio",
        str(audio_dir),
        "--out",
        str(out_dir),
        cwd=checkout,
    )

    produced = sorted(p for p in out_dir.rglob("*") if p.is_file()) if out_dir.is_dir() else []
    if not produced:
        raise RuntimeError(
            f"{TRAIN_ENTRYPOINT} wrote nothing to {out_dir}. A model directory has to be "
            "self-contained: whatever `synthesize.py` needs to speak, plus a config and the "
            "metrics it measured."
        )

    metrics = _read_metrics(out_dir)
    uri = remote.upload_dir(out_dir)
    artifact = remote.Artifact.create(
        Dir.from_existing_remote(uri),
        name=MODEL_ARTIFACT,
        description=(
            f"Voice model from {len(produced)} files. "
            + ", ".join(f"{k}={v}" for k, v in list(metrics.items())[:4])
        )[:255],
        attrs={
            "files": str(len(produced)),
            "source_audio": audio.path,
            "run": flyte.ctx().action.run_name,
            **{k: str(v)[:120] for k, v in metrics.items()},
        },
    )

    await flyte.report.log.aio(
        f"<h3>voice-model {artifact.version}</h3>"
        f"<p>Trained from <code>{audio.path}</code>.</p>"
        f"<pre>{json.dumps(metrics, indent=2)[:3000]}</pre>",
        do_flush=True,
    )
    return f"published {MODEL_ARTIFACT} {artifact.version} from {len(produced)} files"


def _read_metrics(out_dir: Path) -> dict[str, str | int | float]:
    """Lift `metrics.json` into the artifact's attrs, if the trainer wrote one.

    Recorded on the artifact rather than only in the run, so a consumer can see
    what it is about to narrate with -- and so a model that scored badly is
    visible as such without opening the run that made it.
    """
    path = out_dir / "metrics.json"
    if not path.is_file():
        return {}
    try:
        loaded: dict[str, object] = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in loaded.items() if isinstance(v, (str, int, float))}


async def _run(*argv: str, cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    print(text[-8000:], flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {text[-2000:]}")


if __name__ == "__main__":
    flyte.init_from_config()
    run = flyte.run(train, audio=Dir.from_existing_remote("s3://placeholder"))
    print(f"run: {run.url}")
