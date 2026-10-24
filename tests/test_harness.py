"""The orchestrator with the docker SDK, syft and the browser mocked out."""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from contracts import validate

from ci import browser, harness, layers, packages
from ci.browser import ScreenshotResult
from ci.config import load_settings
from ci.packages import SyftError, SyftResult
from tests.conftest import write_unit

TABLE = "NAME     VERSION   TYPE\nbash     5.2       apk\nicu-libs 74.2      apk\n"
LOG_DONE = "[migrations] started\n[ls.io-init] done.\n"


def settings_for(out: Path, enabled: bool, web: bool = False) -> Any:
    manifest: dict[str, Any] = {
        "schema": 1,
        "name": "sonarr",
        "build": {"base": "alpine", "platforms": ["amd64"]},
        "release": {"branch": "master", "tag": "latest", "channel": "stable"},
        "test": {"enabled": enabled, "env": {"FOO": "bar"}},
    }
    if web:
        manifest["test"]["web"] = {"port": 8989, "path": "/", "screenshot_delay": 0, "screenshot_timeout": 5}
    env = {
        "IMAGE": "ghcr.io/linuxserver-playground/sonarr:latest",
        "ARCH": "amd64",
        "MODE": "live",
        "MANIFEST_JSON": json.dumps(manifest),
        "PREVIOUS_PACKAGES_URL": "https://example.invalid/package_versions.amd64.txt",
    }
    return load_settings(env, out_dir=out)


def fake_container(log: str = LOG_DONE) -> MagicMock:
    container = MagicMock()
    container.short_id = "abc123"
    container.status = "running"
    container.logs.return_value = log.encode()
    container.exec_run.return_value = MagicMock(exit_code=0, output=b"VmRSS: 100 kB\n")
    container.diff.return_value = []
    container.attrs = {"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.9"}}}}
    return container


@pytest.fixture
def patched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    syft_json: dict[str, Any],
    inspect_record: dict[str, Any],
    history_records: list[dict[str, Any]],
    s6_tree: Path,
) -> dict[str, Any]:
    calls: dict[str, Any] = {"container": fake_container(), "syft": SyftResult(syft_json, TABLE)}

    def copy_from_image(client: Any, ref: str, src: str, dest: Path) -> bool:
        target = dest / "s6-rc.d"
        target.parent.mkdir(parents=True, exist_ok=True)
        if "baseimage" in ref:
            for unit in ("init-adduser", "init-services", "svc-cron"):
                write_unit(target, unit, "oneshot" if unit != "svc-cron" else "longrun")
        else:
            import shutil

            shutil.copytree(s6_tree, target)
        return True

    monkeypatch.setattr(harness, "inspect_image", lambda client, ref: inspect_record)
    monkeypatch.setattr(harness, "engine_version", lambda client: "29.9.2")
    monkeypatch.setattr(harness, "image_history", lambda client, ref: history_records)
    monkeypatch.setattr(harness, "copy_from_image", copy_from_image)
    monkeypatch.setattr(harness, "export_hardlinks", lambda client, ref: {"/bin/coreutils": "/bin/busybox"})
    monkeypatch.setattr(
        harness, "run_container", lambda client, ref, env: calls.setdefault("env", env) and calls["container"]
    )
    monkeypatch.setattr(harness, "remove_container", lambda container: calls.setdefault("removed", True))
    monkeypatch.setattr(packages, "run_syft", lambda image, timeout: calls["syft"])
    monkeypatch.setattr(packages, "fetch_previous_table", lambda url: "NAME VERSION\nbash 5.1\n")
    monkeypatch.setattr(layers, "registry_manifest_layers", lambda image, arch: None)
    monkeypatch.setattr(
        layers,
        "find_base_image",
        lambda client, inspect, labels, base: (
            "ghcr.io/linuxserver/baseimage-alpine:3.22",
            {"Id": "x", "RootFS": {"Layers": inspect["RootFS"]["Layers"][:2]}},
        ),
    )
    return calls


def run_harness(out: Path, enabled: bool, web: bool = False) -> dict[str, Any]:
    work = out.parent / "work"
    work.mkdir()
    harness.Run(settings_for(out, enabled, web), MagicMock(), work).execute()
    report: dict[str, Any] = json.loads((out / "test.amd64.json").read_text())
    validate("test-report", report)
    return report


def test_full_run_writes_every_asset(tmp_path: Path, patched: dict[str, Any]) -> None:
    out = tmp_path / "out"
    report = run_harness(out, enabled=True)
    assert sorted(p.name for p in out.iterdir()) == [
        "files.amd64.json.gz",
        "image-config.amd64.json",
        "init-graph.amd64.json",
        "layers.amd64.json",
        "package_versions.amd64.txt",
        "packages.amd64.json.gz",
        "test.amd64.json",
        "test.amd64.tar.gz",
    ]
    assert report["success"] is True
    assert [t["name"] for t in report["tests"]] == list(harness.TEST_ORDER)
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Take screenshot"] == "SKIP" and statuses["Dotnet check"] == "PASS"
    assert report["image"] == "sonarr" and report["tag"] == "latest" and report["engine"] == "29.9.2"
    assert report["runtime"]["rss_bytes"] == 102400
    assert patched["env"] == {"S6_VERBOSITY": "2", "FOO": "bar"} and patched["removed"]

    with tarfile.open(out / "test.amd64.tar.gz") as tar:
        names = set(tar.getnames())
        html = tar.extractfile("index.html")
        assert html is not None and b"sonarr:latest" in html.read()
        diff = tar.extractfile("package_diff.txt")
        assert diff is not None and b"[*] bash: 5.1 -> 5.2" in diff.read()
    assert names == {"index.html", "container.log", "sysinfo.txt", "package_diff.txt"}
    assert report["files"] == {"html": "index.html", "container_log": "container.log", "sysinfo": "sysinfo.txt"}

    layers = json.loads((out / "layers.amd64.json").read_text())
    assert [layer["from_base"] for layer in layers] == [True, True, False]
    with gzip.open(out / "files.amd64.json.gz") as fh:
        files = json.load(fh)
    assert {f["path"]: f["type"] for f in files}["/bin/coreutils"] == "hardlink"
    graph = json.loads((out / "init-graph.amd64.json").read_text())
    units = {u["name"]: u for u in graph["units"]}
    assert graph["d2"].startswith('"sonarr:latest": {') and '"baseimage-alpine:3.22"' in graph["d2"]
    assert units["init-adduser"]["source"] == "base"
    assert units["init-sonarr-config"]["source"] == "image" and "script" in units["init-sonarr-config"]


def test_disabled_tests_still_produce_static_assets(tmp_path: Path, patched: dict[str, Any]) -> None:
    out = tmp_path / "out"
    report = run_harness(out, enabled=False)
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Container startup"] == "SKIP" and statuses["Runtime probe"] == "SKIP"
    assert statuses["Layers"] == "PASS" and statuses["File tree"] == "PASS" and statuses["Init graph"] == "PASS"
    assert report["success"] is True
    assert "startup_seconds" not in report and "runtime" not in report
    assert "env" not in patched


def test_startup_failure_and_syft_failure(
    tmp_path: Path, patched: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    patched["container"] = fake_container("still starting")
    patched["container"].status = "exited"

    def failing_syft(image: str, timeout: int) -> SyftResult:
        raise SyftError("syft exploded")

    monkeypatch.setattr(packages, "run_syft", failing_syft)
    out = tmp_path / "out"
    report = run_harness(out, enabled=True)
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Container startup"] == "FAIL"
    assert statuses["Runtime probe"] == "SKIP" and statuses["Take screenshot"] == "SKIP"
    assert statuses["Get package info"] == "FAIL" and statuses["Create SBOM"] == "FAIL"
    assert statuses["File tree"] == "FAIL"
    assert statuses["Layers"] == "PASS"
    assert report["success"] is False
    assert not (out / "packages.amd64.json.gz").exists()
    assert (out / "test.amd64.tar.gz").exists()


def test_screenshot_path(tmp_path: Path, patched: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_screenshot(url: str, out_path: Path, auth: str | None, delay: int, timeout: int) -> Any:
        captured["url"] = url
        out_path.write_bytes(b"\x89PNG")
        return ScreenshotResult(True, "ok", 1.5, '[{"level": "INFO"}]', out_path)

    monkeypatch.setattr(browser, "take_screenshot", fake_screenshot)
    out = tmp_path / "out"
    report = run_harness(out, enabled=True, web=True)
    assert captured["url"] == "http://172.17.0.9:8989/"
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Take screenshot"] == "PASS"
    assert report["files"]["screenshot"] == "screenshot.png" and report["files"]["browser_log"] == "browser.log"


def test_container_start_failure_is_recorded(
    tmp_path: Path, patched: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from docker.errors import APIError

    def boom(client: Any, ref: str, env: dict[str, str]) -> Any:
        raise APIError("no such entrypoint")

    monkeypatch.setattr(harness, "run_container", boom)
    report = run_harness(tmp_path / "out", enabled=True)
    statuses = {t["name"]: t["status"] for t in report["tests"]}
    assert statuses["Container startup"] == "FAIL" and statuses["Create SBOM"] == "PASS"
    assert report["success"] is False
