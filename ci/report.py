"""test.<arch>.json (test-report.schema.json), the HTML report and the tarball that carries it."""

from __future__ import annotations

import json
import logging
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__

logger = logging.getLogger("report")

TEMPLATE_DIR = Path(__file__).resolve().parent
TARBALL_FILES = {
    "html": "index.html",
    "container_log": "container.log",
    "screenshot": "screenshot.png",
    "browser_log": "browser.log",
    "sysinfo": "sysinfo.txt",
}
EXTRA_TARBALL_FILES = ("package_diff.txt",)


@dataclass
class TestResult:
    name: str
    status: str  # PASS, FAIL, SKIP, WARN
    message: str = ""
    seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.message:
            record["message"] = self.message
        if self.seconds is not None:
            record["seconds"] = round(self.seconds, 3)
        return record


@dataclass
class Report:
    image: str
    tag: str
    arch: str
    started: datetime
    finished: datetime | None = None
    tests: list[TestResult] = field(default_factory=list)
    warnings: dict[str, str] = field(default_factory=dict)
    startup_seconds: float | None = None
    runtime: dict[str, Any] = field(default_factory=dict)
    engine: str | None = None
    runner: str | None = None
    files: dict[str, str] = field(default_factory=dict)

    def add(self, result: TestResult) -> TestResult:
        self.tests.append(result)
        return result

    @property
    def success(self) -> bool:
        return all(t.status in ("PASS", "SKIP", "WARN") for t in self.tests) and any(
            t.status == "PASS" for t in self.tests
        )

    def as_dict(self) -> dict[str, Any]:
        finished = self.finished or now()
        record: dict[str, Any] = {
            "schema": 1,
            "image": self.image,
            "tag": self.tag,
            "arch": self.arch,
            "started": rfc3339(self.started),
            "finished": rfc3339(finished),
            "success": self.success,
            "ci_version": __version__,
            "tests": [t.as_dict() for t in self.tests],
        }
        if self.engine:
            record["engine"] = self.engine
        if self.runner:
            record["runner"] = self.runner
        if self.warnings:
            record["warnings"] = dict(self.warnings)
        if self.startup_seconds is not None:
            record["startup_seconds"] = round(self.startup_seconds, 3)
        if self.files:
            record["files"] = dict(self.files)
        if self.runtime:
            record["runtime"] = dict(self.runtime)
        return record


def now() -> datetime:
    return datetime.now(UTC)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_report(record: dict[str, Any]) -> list[str]:
    """Schema violations as strings; empty when valid or when the contracts package is not installed."""
    try:
        from contracts import errors, format_error
    except ImportError:
        logger.debug("contracts package not installed; skipping schema validation")
        return []
    return [format_error(e) for e in errors("test-report", record)]


def render_html(context: dict[str, Any]) -> str:
    env = Environment(
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
    )
    return env.get_template("template.html").render(**context)


def write_tarball(work_dir: Path, tar_path: Path) -> dict[str, str]:
    """Pack the report files that exist in `work_dir` and return the `files` block for test.json."""
    present: dict[str, str] = {}
    with tarfile.open(tar_path, "w:gz") as tar:
        for key, name in TARBALL_FILES.items():
            path = work_dir / name
            if path.is_file():
                tar.add(path, arcname=name)
                present[key] = name
        for name in EXTRA_TARBALL_FILES:
            path = work_dir / name
            if path.is_file():
                tar.add(path, arcname=name)
    return present


def write_json(path: Path, data: Any, indent: int | None = 2) -> None:
    path.write_text(json.dumps(data, indent=indent, sort_keys=False) + "\n", encoding="utf-8")
