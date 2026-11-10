"""Required standards tools must travel with every slot image."""

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
from flyte import Image

from flyte_agent_factory import _images, _scanners
from tests import _standards as template


@pytest.mark.parametrize("browser", [False, True])
def test_every_enforced_tool_is_in_the_slot_image(browser):
    texts = list(template.FILES.values())
    for directory in (Path("standards/languages"), Path(".briefs/standards/languages")):
        texts.extend(path.read_text() for path in directory.rglob("*.md"))
    required = set()
    # The tools the template's `command` rules start with (plus the owner's own copy, when it is here).
    for text in texts:
        for marker in re.findall(r"^Enforced: (?:tool|command) (.+)$", text, re.MULTILINE):
            for part in marker.split(" && "):
                command = shlex.split(part)
                required.add(command[2] if command[:2] == ["python", "-m"] else command[0])
    assert required <= _images.TOOLS.keys()
    layers = _images.agent_image(browser=browser)._layers
    installed = [p for layer in layers for p in getattr(layer, "packages", ())]
    installed += [c for layer in layers for c in getattr(layer, "commands", ())]
    for tool, step in _images.TOOLS.items():
        assert step in installed, tool


def test_downloaded_gate_tools_have_pinned_checksums():
    pins = {
        "terraform": (_images.TERRAFORM_VERSION, _images.TERRAFORM_SHA256),
        "tflint": (_scanners.TFLINT.version, _scanners.TFLINT.sha256),
    }
    for name, (version, sha256) in pins.items():
        assert re.fullmatch(r"[a-f0-9]{64}", sha256), name
        commands = _images.TOOLS[name]
        assert version in commands
        assert sha256 in commands
        assert "sha256sum -c -" in commands


def test_python_gate_tools_are_isolated_and_on_path():
    layers = _images.agent_image()._layers
    commands = [c for layer in layers for c in getattr(layer, "commands", ())]
    assert any("python -m venv /opt/gate-tools" in c for c in commands)
    for name in ("ruff", "mypy", "pytest"):
        assert "/opt/gate-tools/bin/pip install" in _images.TOOLS[name]
        assert f"{name}==" in _images.TOOLS[name]
    paths = [
        dict(env)["PATH"]
        for env in (getattr(layer, "env_vars", ()) for layer in layers)
        if "PATH" in dict(env)
    ]
    assert any(path.endswith("${PATH}:/opt/gate-tools/bin") for path in paths)


def test_the_check_image_writes_quoted_terraform_files():
    # A build failed: the builder stripped the double quotes from a printf'd main.tf, and Terraform could not
    # parse `source = hashicorp/aws`. The files now go in as base64, which has no quotes to lose.
    import base64
    import re

    from flyte_agent_factory import _bench_check

    layers = repr(_bench_check.env.image)
    payloads = [
        base64.b64decode(p).decode() for p in re.findall(r"echo ([A-Za-z0-9+/=]{20,}) \| base64 -d", layers)
    ]
    assert any('source  = "hashicorp/aws"' in p for p in payloads)
    assert any('path = "/opt/bench-terraform/providers"' in p for p in payloads)


def _layer_values(image: Any, attr: str) -> list[Any]:
    return [v for layer in image._layers for v in getattr(layer, attr, ())]


@pytest.mark.parametrize("browser", [False, True])
def test_the_slot_image_builds_with_rootless_podman(browser):
    """The slot image carries rootless podman, with the storage and isolation settings it needs."""
    image = _images.agent_image(browser=browser)
    packages = _layer_values(image, "packages")
    for package in ("podman", "uidmap", "slirp4netns", "fuse-overlayfs"):
        assert package in packages, package
    env = {k: v for layer in image._layers for k, v in dict(getattr(layer, "env_vars", ())).items()}
    assert env["STORAGE_DRIVER"] == "vfs"
    assert env["BUILDAH_ISOLATION"] == "chroot"
    commands = " ".join(_layer_values(image, "commands"))
    # No subordinate id ranges: the cluster refuses newuidmap's multi-range map (EPERM, probed), so
    # podman must fall back to a single-uid mapping, which needs ignore_chown_errors to unpack images.
    assert ": > /etc/subuid" in commands and ": > /etc/subgid" in commands
    assert "flyte:100000" not in commands
    assert env["STORAGE_OPTS"] == "vfs.ignore_chown_errors=true"
    # The published Flyte base is a prebuilt image whose layers cannot be inspected for the user.
    assert "id -u flyte" in commands
    assert "podman --version" in commands


def test_podman_storage_and_containers_conf_are_written_as_base64():
    """Podman's storage and containers config reach the image as base64 payloads."""
    import base64

    commands = " ".join(_layer_values(_images.agent_image(), "commands"))
    payloads = [
        base64.b64decode(p).decode() for p in re.findall(r"echo ([A-Za-z0-9+/=]{20,}) \| base64 -d", commands)
    ]
    assert any('driver = "vfs"' in p for p in payloads)
    # podman refuses to start without both roots in the system storage.conf (a remote build failed on it).
    assert any("runroot = " in p and "graphroot = " in p for p in payloads)
    assert any('cgroup_manager = "cgroupfs"' in p for p in payloads)
    assert "/etc/containers/storage.conf" in commands and "/etc/containers/containers.conf" in commands


def test_every_factory_image_starts_from_the_flyte_debian_base():
    """Every factory image starts from the Flyte Debian base."""
    from flyte_agent_factory import _bench_check

    images = [
        _images.agent_image(),
        _images.agent_image(browser=True),
        _images.browser_image(),
        _bench_check.env.image,
    ]
    for image in images:
        assert isinstance(image, Image)
        assert image.base_image is not None and "3.12" in image.base_image


def test_podman_probe_is_guarded_and_runs_on_the_slot_image():
    """The podman probe is guarded by `__main__` and runs on the slot image."""
    source = Path("ops/probes/podman_rootless.py").read_text()
    assert 'if __name__ == "__main__":' in source
    assert "agent_image" in source
    assert "podman build" in source and "podman run" in source
