"""syft: the package table, the packages-only JSON and SPDX, and the diff against the previous release.

syft is only asked for packages. Everything it knows about individual files (file records, each package's file
list, the relationships pointing at files) is dropped: vulnerability matching never reads it, and on desktop
images it is most of the document. The file tree comes from dumper instead (`dumper.py`)."""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("packages")

SYFT_ENV = {
    "SYFT_FILE_METADATA_SELECTION": "none",
    "SYFT_CHECK_FOR_APP_UPDATE": "false",
}
# BuildKit's SBOM generator adds this cataloger too: it reads the SBOMs that wheels such as Pillow ship for the
# libraries they vendor, which would otherwise be invisible to vulnerability scans.
SYFT_CATALOGERS = "+sbom-cataloger"


class SyftError(RuntimeError):
    pass


@dataclass
class SyftResult:
    data: dict[str, Any]
    table: str


def syft_binary() -> str:
    path = shutil.which("syft")
    if not path:
        raise SyftError("syft binary not found on PATH")
    return path


def trim(doc: dict[str, Any]) -> dict[str, Any]:
    """The packages-only document: no `files`, no per-package `metadata.files`, and only the package-to-package
    relationships (`dependency-of`, and `ownership-by-file-overlap`, which grype uses to drop binary packages an
    OS package owns). Modifies and returns `doc`."""
    ids = {a.get("id") for a in doc.get("artifacts", [])}
    for artifact in doc.get("artifacts", []):
        metadata = artifact.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("files", None)
    doc["artifactRelationships"] = [
        r for r in doc.get("artifactRelationships", []) if r.get("parent") in ids and r.get("child") in ids
    ]
    doc.pop("files", None)
    return doc


def run_syft(image: str, timeout: int, platform: str | None = None) -> SyftResult:
    """One syft run producing both the packages-only JSON and the human table."""
    binary = syft_binary()
    with tempfile.TemporaryDirectory(prefix="syft-") as tmp:
        json_path = Path(tmp) / "sbom.json"
        table_path = Path(tmp) / "table.txt"
        command = [binary, f"docker:{image}", "--select-catalogers", SYFT_CATALOGERS]
        command += ["-o", f"syft-json={json_path}", "-o", f"table={table_path}", "-q"]
        if platform:
            command += ["--platform", platform]
        env = {**os.environ, **SYFT_ENV}
        logger.info("running %s", " ".join(command[:4]))
        try:
            proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as err:
            raise SyftError(f"syft timed out after {timeout}s") from err
        if proc.returncode != 0:
            raise SyftError(f"syft exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
        try:
            data: dict[str, Any] = trim(json.loads(json_path.read_text(encoding="utf-8")))
            table = table_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as err:
            raise SyftError(f"syft produced unreadable output: {err}") from err
    return SyftResult(data=data, table=table)


def write_syft_json(data: dict[str, Any] | list[Any], path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(data, fh, separators=(",", ":"))


def write_spdx(data: dict[str, Any], path: Path, timeout: int = 300) -> None:
    """The same packages-only document as SPDX 2.3 JSON (`syft convert`), for the image attestation. The publish
    job wraps it in the in-toto statement BuildKit uses and attaches it to the released index."""
    binary = syft_binary()
    with tempfile.TemporaryDirectory(prefix="syft-") as tmp:
        source = Path(tmp) / "sbom.json"
        source.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        command = [binary, "convert", str(source), "-o", f"spdx-json={path}", "-q"]
        env = {**os.environ, **SYFT_ENV}
        try:
            proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as err:
            raise SyftError(f"syft convert timed out after {timeout}s") from err
        if proc.returncode != 0:
            raise SyftError(f"syft convert exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")


def package_count(data: dict[str, Any]) -> int:
    artifacts = data.get("artifacts")
    return len(artifacts) if isinstance(artifacts, list) else 0


def parse_table(text: str) -> dict[str, str]:
    """NAME VERSION [TYPE] table -> {name: version}. Duplicate names keep the first version seen."""
    packages: dict[str, str] = {}
    lines = text.strip().splitlines()
    if lines and lines[0].split()[:2] == ["NAME", "VERSION"]:
        lines = lines[1:]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in packages:
            packages[parts[0]] = parts[1]
    return packages


def diff_text(old: dict[str, str], new: dict[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is None:
            lines.append(f"[+] {name}: {after} (Added)")
        elif after is None:
            lines.append(f"[-] {name}: {before} (Removed)")
        elif before != after:
            lines.append(f"[*] {name}: {before} -> {after} (Changed)")
    return "\n".join(lines) if lines else "No package changes found."


def fetch_previous_table(url: str, timeout: int = 30) -> str | None:
    """The previous release's package_versions.<arch>.txt, or None when it cannot be fetched."""
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as err:
        logger.warning("could not fetch %s: %s", url, err)
        return None
    if response.status_code == 404:
        logger.info("no previous package list at %s", url)
        return None
    if not response.ok:
        logger.warning("previous package list at %s returned %s", url, response.status_code)
        return None
    return response.text


def package_diff(previous_url: str | None, current_table: str) -> str:
    if not previous_url:
        return "No previous release to compare against."
    previous = fetch_previous_table(previous_url)
    if previous is None:
        return f"Previous package list unavailable ({previous_url})."
    return diff_text(parse_table(previous), parse_table(current_table))
