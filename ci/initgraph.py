"""The s6-rc init graph: units read from /etc/s6-overlay/s6-rc.d and the d2 diagram in the readme style.

A unit is this image's own when the repository ships it (`root/etc/s6-overlay/s6-rc.d/<name>`, the directory the
workflow mounts as IMAGE_ROOT); every other unit came with a base image, however many bases deep. Without the
repository, the base image's units are compared when it is in the local store, and the units every root base
ships are assumed otherwise. Only own units carry their run script: the website reads a base unit's script from
the base image's own build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNIT_TYPES = ("oneshot", "longrun", "bundle")

# Units every root base image ships. The last resort when neither the repository nor the base image is at hand:
# it cannot see the units of a base built on another base (selkies on ubuntu), which then count as the image's own.
KNOWN_BASE_UNITS = frozenset(
    {
        "ci-service-check",
        "init-adduser",
        "init-config",
        "init-config-end",
        "init-crontab-config",
        "init-custom-files",
        "init-device-perms",
        "init-envfile",
        "init-migrations",
        "init-mods",
        "init-mods-end",
        "init-mods-package-install",
        "init-os-end",
        "init-services",
        "svc-cron",
        "user",
        "user2",
    }
)


@dataclass
class Unit:
    name: str
    type: str
    depends_on: list[str] = field(default_factory=list)
    contents: list[str] = field(default_factory=list)
    run: str | None = None
    up: str | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _names(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))


def read_units(root: Path) -> list[Unit]:
    """One Unit per directory under `root` that has a valid `type` file."""
    units: list[Unit] = []
    if not root.is_dir():
        return units
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        kind = (_read_text(entry / "type") or "").strip()
        if kind not in UNIT_TYPES:
            continue
        units.append(
            Unit(
                name=entry.name,
                type=kind,
                depends_on=_names(entry / "dependencies.d"),
                contents=_names(entry / "contents.d"),
                run=_read_text(entry / "run"),
                up=_read_text(entry / "up"),
            )
        )
    return units


def classify(name: str, base_names: frozenset[str] | set[str] | None, own_names: set[str] | None = None) -> str:
    """`mod`, `image` or `base`: the repository's own units when known (`own_names`), else whatever is not a base
    unit, from the base image's list when it was compared and the known root-base units otherwise."""
    if name.startswith(("init-mod-", "svc-mod-")):
        return "mod"
    if own_names is not None:
        return "image" if name in own_names else "base"
    known = base_names if base_names is not None else KNOWN_BASE_UNITS
    return "base" if name in known else "image"


def unit_records(
    units: list[Unit], base_names: set[str] | None, own_names: set[str] | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for unit in units:
        source = classify(unit.name, base_names, own_names)
        record: dict[str, Any] = {
            "name": unit.name,
            "type": unit.type,
            "depends_on": list(unit.depends_on),
            "source": source,
        }
        if source == "image":
            script = unit.run if unit.run is not None else unit.up
            if script:
                record["script"] = script
        records.append(record)
    return records


def render_d2(units: list[Unit], header: str, base_images: list[str]) -> str:
    """The diagram in the exact shape of the `init_diagram` blocks the Jenkins pipeline wrote into readme-vars.yml."""
    lines = [
        f'"{header}": {{',
        "  docker-mods",
        "  base {",
        "    fix-attr +\\nlegacy cont-init",
        "  }",
        "  docker-mods -> base",
        "  legacy-services",
        "  custom services",
        "  init-services -> legacy-services",
        "  init-services -> custom services",
        "  custom services -> legacy-services",
        "  legacy-services -> ci-service-check",
    ]
    for unit in sorted(units, key=lambda u: u.name):
        if unit.type == "bundle" or unit.name == "ci-service-check":
            continue
        if unit.depends_on:
            lines.extend(f"  {dep} -> {unit.name}" for dep in unit.depends_on)
        else:
            lines.append(f"  base -> {unit.name}")
        if unit.type == "longrun":
            lines.append(f"  {unit.name} -> legacy-services")
    lines.append("}")
    lines.append("Base Images: {")
    lines.extend(f'  "{image}"' for image in base_images)
    lines.append("}")
    lines.append(f'"{header}" <- Base Images')
    return "\n".join(lines) + "\n"


def short_image_name(ref: str) -> str:
    """ghcr.io/linuxserver/baseimage-alpine:3.22 -> baseimage-alpine:3.22"""
    return ref.split("@", 1)[0].rsplit("/", 1)[-1]


def build_init_graph(
    units: list[Unit],
    header: str,
    base_images: list[str],
    base_names: set[str] | None,
    own_names: set[str] | None = None,
) -> dict[str, Any]:
    return {"d2": render_d2(units, header, base_images), "units": unit_records(units, base_names, own_names)}
