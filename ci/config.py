"""Environment and manifest parsing. Everything the harness needs is decided here, once."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Arch = Literal["amd64", "arm64"]
Mode = Literal["live", "dev", "pr"]


DESKTOP_BASES = ("selkies", "kasmvnc", "rdesktop")
DESKTOP_SCREENSHOT_DELAY: dict[str, int] = {"amd64": 30, "arm64": 90}
"""Desktop images draw their session after the page has loaded, slower on arm64: the least they are given."""
SCREENSHOT_MARGIN = 60
"""The screenshot timeout always leaves this long for the endpoint to answer on top of the delay."""


class ConfigError(ValueError):
    """The harness cannot run with the environment it was given."""


class WebTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(ge=1, le=65535)
    path: str = "/"
    scheme: Literal["http", "https"] = "http"
    auth: str | None = None
    screenshot_delay: int = 10
    screenshot_timeout: int = 120


class TestSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    startup_timeout: int = Field(default=120, ge=10, le=1800)
    env: dict[str, str] = Field(default_factory=dict)
    web: WebTest | None = None
    sbom_timeout: int = 900


def _default_platforms() -> list[Arch]:
    return ["amd64", "arm64"]


class BuildSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base: str = "other"
    platforms: list[Arch] = Field(default_factory=_default_platforms)


class ReleaseSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branch: str = ""
    tag: str = "latest"


class PortSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    internal: str
    optional: bool = False

    @property
    def number(self) -> int:
        return int(self.internal.split("/", 1)[0])

    @property
    def protocol(self) -> Literal["tcp", "udp"]:
        return "udp" if self.internal.endswith("/udp") else "tcp"


class RuntimeSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ports: list[PortSpec] = Field(default_factory=list)


class Manifest(BaseModel):
    """The parts of image.yml the harness reads. Unknown sections are ignored, not rejected: the
    manifest is validated against image.schema.json by the builder before it reaches us."""

    model_config = ConfigDict(extra="ignore")

    name: str
    build: BuildSection = Field(default_factory=BuildSection)
    release: ReleaseSection = Field(default_factory=ReleaseSection)
    test: TestSection = Field(default_factory=TestSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)

    @property
    def required_ports(self) -> list[PortSpec]:
        """Ports the image declares as always listening: everything not marked optional."""
        return [p for p in self.runtime.ports if not p.optional]


@dataclass(frozen=True)
class Settings:
    image: str
    arch: Arch
    mode: Mode
    manifest: Manifest
    out_dir: Path
    previous_packages_url: str | None
    log_level: str
    syft_timeout: int
    startup_timeout: int
    screenshot: bool

    @property
    def repository(self) -> str:
        """The image reference without its tag."""
        return split_ref(self.image)[0]

    @property
    def tag(self) -> str:
        return split_ref(self.image)[1]

    @property
    def web(self) -> WebTest | None:
        return self.manifest.test.web if self.screenshot else None

    @property
    def screenshot_delay(self) -> int:
        """Seconds between the page loading and the screenshot: the manifest's, raised for desktop bases."""
        web = self.manifest.test.web
        configured = web.screenshot_delay if web else 0
        if self.manifest.build.base in DESKTOP_BASES:
            return max(configured, DESKTOP_SCREENSHOT_DELAY[self.arch])
        return configured

    @property
    def screenshot_timeout(self) -> int:
        web = self.manifest.test.web
        configured = web.screenshot_timeout if web else 0
        return max(configured, self.screenshot_delay + SCREENSHOT_MARGIN)


def split_ref(ref: str) -> tuple[str, str]:
    """'ghcr.io/org/name:tag' -> ('ghcr.io/org/name', 'tag'); a digest reference keeps the digest as tag."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, digest
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > last_slash:
        return ref[:colon], ref[colon + 1 :]
    return ref, "latest"


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from err


def load_settings(env: Mapping[str, str], out_dir: Path = Path("/out")) -> Settings:
    """Build Settings from the process environment. Raises ConfigError with a readable message."""
    image = env.get("IMAGE", "").strip()
    if not image:
        raise ConfigError("IMAGE is required")
    arch = env.get("ARCH", "").strip()
    if arch not in ("amd64", "arm64"):
        raise ConfigError(f"ARCH must be amd64 or arm64, got {arch!r}")
    mode = env.get("MODE", "").strip()
    if mode not in ("live", "dev", "pr"):
        raise ConfigError(f"MODE must be live, dev or pr, got {mode!r}")
    raw_manifest = env.get("MANIFEST_JSON", "").strip()
    if not raw_manifest:
        raise ConfigError("MANIFEST_JSON is required")
    try:
        manifest = Manifest.model_validate(json.loads(raw_manifest))
    except json.JSONDecodeError as err:
        raise ConfigError(f"MANIFEST_JSON is not valid JSON: {err}") from err
    except ValidationError as err:
        raise ConfigError(f"MANIFEST_JSON does not describe a usable manifest: {err}") from err

    previous = env.get("PREVIOUS_PACKAGES_URL", "").strip() or None
    screenshot = env.get("SCREENSHOT", "true").strip().lower() != "false"
    return Settings(
        image=image,
        arch=arch,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        manifest=manifest,
        out_dir=out_dir,
        previous_packages_url=previous,
        log_level=env.get("CI_LOG_LEVEL", "INFO").strip() or "INFO",
        syft_timeout=_int_env(env, "SYFT_TIMEOUT", manifest.test.sbom_timeout),
        startup_timeout=_int_env(env, "STARTUP_TIMEOUT", manifest.test.startup_timeout),
        screenshot=screenshot,
    )
