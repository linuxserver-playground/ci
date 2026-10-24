"""The file tree: bundle.schema.json $defs/file_entry records derived from the syft JSON."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

TYPE_MAP = {
    "RegularFile": "file",
    "Directory": "dir",
    "SymbolicLink": "symlink",
    "HardLink": "hardlink",
    "CharacterDevice": "device",
    "BlockDevice": "device",
}

# Go os.FileMode bits as syft serialises them (the decimal digits are the octal representation).
_SETUID = 1 << 23
_SETGID = 1 << 22
_STICKY = 1 << 20


def mode_string(mode: int) -> str:
    """syft writes Go's FileMode as octal digits in a decimal field: 755 -> '0755', 1000000777 -> '0777'."""
    try:
        fm = int(str(mode), 8)
    except ValueError:
        return "0000"
    special = (4 if fm & _SETUID else 0) + (2 if fm & _SETGID else 0) + (1 if fm & _STICKY else 0)
    return f"{special}{fm & 0o777:03o}"


def _sha256(entry: dict[str, Any]) -> str | None:
    for digest in entry.get("digests") or []:
        if digest.get("algorithm") == "sha256" and digest.get("value"):
            return str(digest["value"])
    return None


def _purl_by_file(data: dict[str, Any]) -> dict[str, str]:
    purls = {a["id"]: a.get("purl") for a in data.get("artifacts", []) if a.get("id") and a.get("purl")}
    owner: dict[str, str] = {}
    for rel in data.get("artifactRelationships", []):
        if rel.get("type") != "contains":
            continue
        purl = purls.get(rel.get("parent"))
        child = rel.get("child")
        if purl and child and child not in owner:
            owner[child] = purl
    return owner


def build_file_entries(data: dict[str, Any], diff_ids: list[str]) -> list[dict[str, Any]]:
    """One file_entry per syft file record. Regular files that share (layer, sha256) are hardlinks of each other:
    the first path (sorted) stays `file`, the rest become `hardlink` pointing at it so sizes are not double counted."""
    layer_index = {diff_id: i for i, diff_id in enumerate(diff_ids)}
    owner = _purl_by_file(data)
    entries: list[dict[str, Any]] = []
    for record in data.get("files", []):
        location = record.get("location", {})
        path = location.get("path")
        if not path:
            continue
        layer_id = location.get("layerID", "")
        if layer_id not in layer_index:
            layer_index[layer_id] = len(layer_index)
        meta = record.get("metadata") or {}
        kind = TYPE_MAP.get(str(meta.get("type", "")), "other")
        entry: dict[str, Any] = {"path": path, "layer": layer_index[layer_id], "type": kind}
        if "size" in meta and kind != "dir":
            entry["size"] = int(meta["size"])
        if "mode" in meta:
            entry["mode"] = mode_string(int(meta["mode"]))
        if "userID" in meta:
            entry["uid"] = int(meta["userID"])
        if "groupID" in meta:
            entry["gid"] = int(meta["groupID"])
        digest = _sha256(record)
        if digest:
            entry["digest"] = digest[:16]
        if meta.get("linkDestination"):
            entry["link_target"] = str(meta["linkDestination"])
        if meta.get("mimeType"):
            entry["mime"] = str(meta["mimeType"])
        purl = owner.get(record.get("id", ""))
        if purl:
            entry["package"] = purl
        if kind == "file":
            is_elf = isinstance(record.get("executable"), dict)
            has_x = bool(int(str(meta.get("mode", 0)), 8) & 0o111) if "mode" in meta else False
            entry["executable"] = is_elf or has_x
            entry["_sha256"] = digest
        entries.append(entry)
    entries.sort(key=lambda e: str(e["path"]))
    _dedupe_hardlinks(entries)
    for entry in entries:
        entry.pop("_sha256", None)
    return entries


def _dedupe_hardlinks(entries: list[dict[str, Any]]) -> None:
    seen: dict[tuple[int, str], str] = {}
    groups: defaultdict[tuple[int, str], int] = defaultdict(int)
    for entry in entries:
        sha = entry.get("_sha256")
        if entry["type"] != "file" or not sha or not entry.get("size"):
            continue
        key = (int(entry["layer"]), sha)
        groups[key] += 1
        first = seen.get(key)
        if first is None:
            seen[key] = str(entry["path"])
            continue
        entry["type"] = "hardlink"
        entry["link_target"] = first


def total_size(entries: list[dict[str, Any]]) -> int:
    return sum(int(e.get("size", 0)) for e in entries if e["type"] == "file")
