"""The whole harness, in-process, against the Alpine base image. Needs a Docker daemon, syft and dumper."""

from __future__ import annotations

import gzip
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from contracts import validate

from ci.config import load_settings
from ci.dumper import DumperError, dumper_binary
from ci.harness import run

IMAGE = "ghcr.io/linuxserver/baseimage-alpine:3.22"
MANIFEST = {
    "schema": 1,
    "name": "baseimage-alpine",
    "build": {"base": "alpine", "platforms": ["amd64", "arm64"]},
    "release": {"branch": "master", "tag": "3.22", "channel": "stable"},
    "test": {"enabled": True, "startup_timeout": 60},
}
EXPECTED = [
    "files.amd64.json.gz",
    "image-config.amd64.json",
    "init-graph.amd64.json",
    "layers.amd64.json",
    "package_versions.amd64.txt",
    "packages.amd64.json.gz",
    "sbom.amd64.spdx.json",
    "test.amd64.json",
    "test.amd64.tar.gz",
]


def docker_ready() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.pull(IMAGE)
        return True
    except Exception:
        return False


@pytest.mark.integration
def test_whole_harness_against_the_base_image(tmp_path: Path) -> None:
    if shutil.which("syft") is None:
        pytest.skip("syft is not on PATH")
    try:
        dumper_binary()
    except DumperError:
        pytest.skip("dumper is not on PATH and DUMPER is not set")
    if not docker_ready():
        pytest.skip("Docker daemon not reachable or image pull failed")
    out = tmp_path / "out"
    settings = load_settings(
        {"IMAGE": IMAGE, "ARCH": "amd64", "MODE": "live", "MANIFEST_JSON": json.dumps(MANIFEST)}, out_dir=out
    )
    assert run(settings) == 0
    assert sorted(p.name for p in out.iterdir()) == EXPECTED

    report = json.loads((out / "test.amd64.json").read_text())
    validate("test-report", report)
    assert report["success"] is True
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Container startup"] == "PASS"
    assert statuses["Take screenshot"] == "SKIP"
    assert statuses["Runtime probe"] == "PASS"
    assert report["runtime"]["s6_services"] == ["svc-cron"]
    assert report["startup_seconds"] > 0

    with tarfile.open(out / "test.amd64.tar.gz") as tar:
        names = set(tar.getnames())
    assert {"container.log", "sysinfo.txt", "package_diff.txt"} <= names
    assert report["files"] == {"container_log": "container.log", "sysinfo": "sysinfo.txt"}

    layers = json.loads((out / "layers.amd64.json").read_text())
    inspect = json.loads((out / "image-config.amd64.json").read_text())
    assert [layer["diff_id"] for layer in layers] == inspect["RootFS"]["Layers"]
    with gzip.open(out / "files.amd64.json.gz") as fh:
        files = json.load(fh)
    assert files["format"] == "lsio-files/1"
    bin_dir = next(c for c in files["tree"]["c"] if c["n"] == "bin")
    assert any(c["n"] == "busybox" and c.get("m") == 0o755 and c.get("s") for c in bin_dir["c"])
    assert not any(c["n"] == ".lsio-dumper" for c in files["tree"]["c"])
    graph = json.loads((out / "init-graph.amd64.json").read_text())
    assert graph["d2"].startswith('"baseimage-alpine:3.22": {')
    assert {u["name"] for u in graph["units"]} >= {"init-adduser", "svc-cron", "user"}
    with gzip.open(out / "packages.amd64.json.gz") as fh:
        packages = json.load(fh)
    assert packages["artifacts"] and "files" not in packages
    assert json.loads((out / "sbom.amd64.spdx.json").read_text())["spdxVersion"] == "SPDX-2.3"
    assert "busybox" in (out / "package_versions.amd64.txt").read_text()
