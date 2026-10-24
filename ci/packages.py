"""syft: the package table, the full JSON with file metadata, and the diff against the previous release."""

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
    "SYFT_FILE_METADATA_SELECTION": "all",
    "SYFT_FILE_METADATA_DIGESTS": "sha256",
    "SYFT_CHECK_FOR_APP_UPDATE": "false",
}


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


def run_syft(image: str, timeout: int, platform: str | None = None) -> SyftResult:
    """One syft run producing both the JSON (with file metadata) and the human table."""
    binary = syft_binary()
    with tempfile.TemporaryDirectory(prefix="syft-") as tmp:
        json_path = Path(tmp) / "sbom.json"
        table_path = Path(tmp) / "table.txt"
        command = [binary, f"docker:{image}", "-o", f"syft-json={json_path}", "-o", f"table={table_path}", "-q"]
        if platform:
            command += ["--platform", platform]
        env = {**os.environ, **SYFT_ENV}
        logger.info("running %s", " ".join(command[:2] + command[6:]))
        try:
            proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as err:
            raise SyftError(f"syft timed out after {timeout}s") from err
        if proc.returncode != 0:
            raise SyftError(f"syft exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
        try:
            data: dict[str, Any] = json.loads(json_path.read_text(encoding="utf-8"))
            table = table_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as err:
            raise SyftError(f"syft produced unreadable output: {err}") from err
    return SyftResult(data=data, table=table)


def write_syft_json(data: dict[str, Any] | list[Any], path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(data, fh, separators=(",", ":"))


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
