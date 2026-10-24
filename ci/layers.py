"""Layers: bundle.schema.json $defs/layer records from docker inspect, docker history and the registry manifest."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from docker import DockerClient

from .docker_client import local_image_tags, try_inspect_image

logger = logging.getLogger("layers")

LAYER_INSTRUCTIONS = ("RUN", "COPY", "ADD", "WORKDIR")


def diff_ids(inspect: dict[str, Any]) -> list[str]:
    layers = inspect.get("RootFS", {}).get("Layers") or []
    return [str(layer) for layer in layers]


def _instruction(created_by: str) -> str:
    text = created_by.strip()
    if text.startswith("/bin/sh -c #(nop)"):
        text = text[len("/bin/sh -c #(nop)") :].strip()
    elif text.startswith("/bin/sh -c"):
        return "RUN"
    return text.split(" ", 1)[0].upper() if text else ""


def clean_created_by(created_by: str) -> str:
    text = created_by.strip()
    if text.endswith("# buildkit"):
        text = text[: -len("# buildkit")].rstrip()
    if text.startswith("/bin/sh -c #(nop)"):
        text = text[len("/bin/sh -c #(nop)") :].strip()
    elif text.startswith("/bin/sh -c "):
        text = "RUN " + text[len("/bin/sh -c ") :]
    return text


def match_history(history: list[dict[str, Any]], count: int) -> list[dict[str, Any] | None]:
    """Pick the `count` history entries (oldest first) that produced filesystem layers.

    Prefers entries with a non-zero size, then layer-producing instructions, and gives up (all None) when the
    history cannot be reconciled with the layer list."""
    oldest_first = list(reversed(history))
    by_size = [h for h in oldest_first if int(h.get("Size", 0) or 0) > 0]
    if len(by_size) == count:
        return list(by_size)
    by_instruction = [
        h
        for h in oldest_first
        if int(h.get("Size", 0) or 0) > 0 or _instruction(str(h.get("CreatedBy", ""))) in LAYER_INSTRUCTIONS
    ]
    if len(by_instruction) == count:
        return list(by_instruction)
    if len(by_instruction) > count:
        return list(by_instruction[-count:])
    logger.warning("history has %s layer entries but the image has %s layers", len(by_instruction), count)
    return [None] * count


def build_layers(
    inspect: dict[str, Any],
    history: list[dict[str, Any]],
    base_inspect: dict[str, Any] | None = None,
    manifest_layers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ids = diff_ids(inspect)
    matched = match_history(history, len(ids))
    base_ids = diff_ids(base_inspect) if base_inspect else []
    base_is_prefix = bool(base_ids) and ids[: len(base_ids)] == base_ids and len(base_ids) < len(ids)
    compressed = manifest_layers if manifest_layers and len(manifest_layers) == len(ids) else None
    if manifest_layers and compressed is None:
        logger.warning("registry manifest has %s layers, image has %s; ignoring it", len(manifest_layers), len(ids))

    layers: list[dict[str, Any]] = []
    for index, diff_id in enumerate(ids):
        entry = matched[index]
        uncompressed = int(entry.get("Size", 0) or 0) if entry else 0
        layer: dict[str, Any] = {"index": index, "diff_id": diff_id, "uncompressed_size": uncompressed}
        if compressed:
            layer["digest"] = str(compressed[index]["digest"])
            layer["size"] = int(compressed[index]["size"])
        else:
            layer["digest"] = diff_id
            layer["size"] = uncompressed
        if entry and entry.get("CreatedBy"):
            layer["created_by"] = clean_created_by(str(entry["CreatedBy"]))
        if base_is_prefix:
            layer["from_base"] = index < len(base_ids)
        layer["empty"] = uncompressed == 0
        layers.append(layer)
    return layers


def base_image_ref(labels: dict[str, str], base_family: str) -> str | None:
    """The FROM reference as the shared workflow records it, without the digest."""
    ref = labels.get("io.linuxserver.image.base", "").strip()
    if ref:
        return ref.split("@", 1)[0]
    if base_family and base_family not in ("other", "scratch"):
        return f"ghcr.io/linuxserver/baseimage-{base_family}"
    return None


def find_base_image(
    client: DockerClient, inspect: dict[str, Any], labels: dict[str, str], base_family: str
) -> tuple[str, dict[str, Any]] | None:
    """The local image whose layers are the longest proper prefix of this image's layers.

    The labelled FROM reference is tried first; every local `baseimage-*` tag is a candidate after that."""
    ids = diff_ids(inspect)
    candidates: list[str] = []
    labelled = base_image_ref(labels, base_family)
    if labelled and ":" in labelled.rsplit("/", 1)[-1]:
        candidates.append(labelled)
    for tags in local_image_tags(client).values():
        candidates.extend(t for t in tags if "baseimage-" in t)
    best: tuple[str, dict[str, Any]] | None = None
    best_len = 0
    for ref in dict.fromkeys(candidates):
        attrs = try_inspect_image(client, ref)
        if not attrs or attrs.get("Id") == inspect.get("Id"):
            continue
        base_ids = diff_ids(attrs)
        if base_ids and len(base_ids) < len(ids) and ids[: len(base_ids)] == base_ids and len(base_ids) > best_len:
            best, best_len = (ref, attrs), len(base_ids)
    return best


def registry_manifest_layers(image: str, arch: str, timeout: int = 60) -> list[dict[str, Any]] | None:
    """Compressed layer digests and sizes via `docker buildx imagetools inspect --raw`; None when not pushed."""
    docker_cli = shutil.which("docker")
    if not docker_cli:
        return None

    def raw(ref: str) -> dict[str, Any] | None:
        try:
            proc = subprocess.run(
                [docker_cli, "buildx", "imagetools", "inspect", "--raw", ref],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            logger.info("registry manifest for %s not available: %s", ref, proc.stderr.strip()[:200])
            return None
        try:
            parsed: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return parsed

    manifest = raw(image)
    if manifest is None:
        return None
    if "manifests" in manifest:
        repo = image.split("@", 1)[0]
        if ":" in repo.rsplit("/", 1)[-1]:
            repo = repo.rsplit(":", 1)[0]
        for item in manifest["manifests"]:
            platform = item.get("platform", {})
            if platform.get("architecture") == arch and platform.get("os", "linux") == "linux":
                manifest = raw(f"{repo}@{item['digest']}")
                break
        else:
            return None
    if manifest is None or "layers" not in manifest:
        return None
    return [{"digest": layer["digest"], "size": int(layer["size"])} for layer in manifest["layers"]]
