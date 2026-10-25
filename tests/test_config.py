import json
from pathlib import Path

import pytest

from ci.config import ConfigError, load_settings, split_ref

MANIFEST = {
    "schema": 1,
    "name": "sonarr",
    "build": {"base": "alpine", "platforms": ["amd64", "arm64"]},
    "test": {"startup_timeout": 240, "web": {"port": 8989, "path": "/system/status"}, "env": {"FOO": "bar"}},
    "runtime": {"ports": [{"internal": "8989", "description": "WebUI"}]},
    "docs": {"categories": ["Media Management"]},
}


def env(**overrides: str) -> dict[str, str]:
    base = {
        "IMAGE": "ghcr.io/linuxserver-playground/sonarr:amd64-4.0.20-ls1",
        "ARCH": "amd64",
        "MODE": "live",
        "MANIFEST_JSON": json.dumps(MANIFEST, separators=(",", ":")),
    }
    base.update(overrides)
    return base


def test_settings_from_env() -> None:
    settings = load_settings(env(PREVIOUS_PACKAGES_URL="https://example.invalid/package_versions.amd64.txt"))
    assert settings.repository == "ghcr.io/linuxserver-playground/sonarr"
    assert settings.tag == "amd64-4.0.20-ls1"
    assert settings.manifest.name == "sonarr"
    assert settings.startup_timeout == 240
    assert settings.syft_timeout == 900
    assert settings.manifest.test.env == {"FOO": "bar"}
    assert settings.web is not None and settings.web.port == 8989 and settings.web.path == "/system/status"
    assert settings.previous_packages_url == "https://example.invalid/package_versions.amd64.txt"
    assert settings.out_dir == Path("/out")


def test_overrides() -> None:
    settings = load_settings(env(STARTUP_TIMEOUT="30", SYFT_TIMEOUT="120", SCREENSHOT="false", CI_LOG_LEVEL="debug"))
    assert settings.startup_timeout == 30
    assert settings.syft_timeout == 120
    assert settings.web is None
    assert settings.manifest.test.web is not None
    assert settings.log_level == "debug"


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("IMAGE", "", "IMAGE"),
        ("ARCH", "armhf", "ARCH"),
        ("MODE", "nightly", "MODE"),
        ("MANIFEST_JSON", "", "MANIFEST_JSON"),
        ("MANIFEST_JSON", "{not json", "valid JSON"),
        ("MANIFEST_JSON", '{"schema": 1}', "usable manifest"),
        ("STARTUP_TIMEOUT", "soon", "STARTUP_TIMEOUT"),
    ],
)
def test_bad_env_is_a_config_error(key: str, value: str, fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env(**{key: value}))
    assert fragment in str(excinfo.value)


def test_unknown_manifest_sections_are_ignored_but_unknown_test_keys_are_not() -> None:
    manifest = dict(MANIFEST)
    manifest["test"] = {"web": {"port": 80, "bogus": 1}}
    with pytest.raises(ConfigError):
        load_settings(env(MANIFEST_JSON=json.dumps(manifest)))


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("ghcr.io/org/name:tag", ("ghcr.io/org/name", "tag")),
        ("localhost:5000/name", ("localhost:5000/name", "latest")),
        ("name", ("name", "latest")),
        ("ghcr.io/org/name@sha256:abc", ("ghcr.io/org/name", "sha256:abc")),
    ],
)
def test_split_ref(ref: str, expected: tuple[str, str]) -> None:
    assert split_ref(ref) == expected


@pytest.mark.parametrize(
    ("base", "arch", "configured", "delay", "timeout"),
    [
        ("alpine", "arm64", 10, 10, 120),
        ("selkies", "amd64", 10, 30, 120),
        ("selkies", "arm64", 30, 90, 150),
        ("kasmvnc", "arm64", 120, 120, 180),
    ],
)
def test_desktop_bases_wait_longer_before_the_screenshot(
    base: str, arch: str, configured: int, delay: int, timeout: int
) -> None:
    manifest = {**MANIFEST, "build": {"base": base, "platforms": ["amd64", "arm64"]}}
    manifest["test"] = {"web": {"port": 3001, "screenshot_delay": configured}}
    settings = load_settings(env(ARCH=arch, MANIFEST_JSON=json.dumps(manifest)))
    assert (settings.screenshot_delay, settings.screenshot_timeout) == (delay, timeout)
