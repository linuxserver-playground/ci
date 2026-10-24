"""Shared fixtures: a syft JSON document, docker inspect and history records, and an s6-rc.d tree."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

LAYER_A = "sha256:" + "a" * 64
LAYER_B = "sha256:" + "b" * 64
LAYER_C = "sha256:" + "c" * 64


def syft_file(
    path: str,
    layer: str,
    kind: str = "RegularFile",
    size: int = 0,
    mode: int = 644,
    sha: str | None = None,
    link: str | None = None,
    mime: str = "",
    elf: bool = False,
    file_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": file_id or f"id-{path}",
        "location": {"path": path, "layerID": layer},
        "metadata": {"mode": mode, "type": kind, "userID": 0, "groupID": 0, "mimeType": mime, "size": size},
    }
    if link:
        record["metadata"]["linkDestination"] = link
    if sha:
        record["digests"] = [{"algorithm": "sha256", "value": sha}]
    if elf:
        record["executable"] = {"format": "elf", "hasExports": True}
    return record


@pytest.fixture
def syft_json() -> dict[str, Any]:
    """A busybox-like multicall binary with three hardlinks, identical-content files that are not hardlinks,
    a symlink, a directory, a device and a package-owned file."""
    busybox_sha = "6d" * 32
    type_sha = "aa" * 32
    return {
        "artifacts": [
            {
                "id": "pkg-busybox",
                "name": "busybox",
                "version": "1.37.0-r20",
                "type": "apk",
                "purl": "pkg:apk/alpine/busybox@1.37.0-r20",
            },
            {"id": "pkg-nopurl", "name": "weird", "version": "1", "type": "binary"},
        ],
        "artifactRelationships": [
            {"parent": "pkg-busybox", "child": "id-/bin/busybox", "type": "contains"},
            {"parent": "pkg-busybox", "child": "id-/bin/sh", "type": "contains"},
            {"parent": "pkg-nopurl", "child": "id-/opt/weird", "type": "contains"},
            {"parent": "pkg-busybox", "child": "id-/bin/busybox", "type": "evident-by"},
        ],
        "files": [
            syft_file("/bin", LAYER_A, "Directory", mode=755),
            syft_file(
                "/bin/busybox",
                LAYER_A,
                size=808712,
                mode=755,
                sha=busybox_sha,
                mime="application/x-sharedlib",
                elf=True,
            ),
            syft_file(
                "/bin/coreutils",
                LAYER_A,
                size=808712,
                mode=755,
                sha=busybox_sha,
                mime="application/x-sharedlib",
                elf=True,
            ),
            syft_file(
                "/usr/bin/[", LAYER_A, size=808712, mode=755, sha=busybox_sha, mime="application/x-sharedlib", elf=True
            ),
            syft_file("/bin/sh", LAYER_A, "SymbolicLink", mode=1000000777, link="/bin/busybox"),
            syft_file("/dev/null", LAYER_A, "CharacterDevice", mode=2000000666),
            syft_file("/etc/s6-overlay/s6-rc.d/init-a/type", LAYER_B, size=8, sha=type_sha, mime="text/plain"),
            syft_file("/etc/s6-overlay/s6-rc.d/init-b/type", LAYER_B, size=8, sha=type_sha, mime="text/plain"),
            syft_file(
                "/etc/s6-overlay/s6-rc.d/init-b/run",
                LAYER_B,
                size=120,
                mode=755,
                sha="cc" * 32,
                mime="text/x-shellscript",
            ),
            syft_file(
                "/usr/bin/sudo",
                LAYER_C,
                size=1000,
                mode=40000755,
                sha="dd" * 32,
                mime="application/x-sharedlib",
                elf=True,
            ),
            syft_file("/opt/weird", LAYER_C, size=5, mode=600, sha="ee" * 32),
            syft_file("/run/lock", LAYER_C, "FIFO", mode=600),
            {"id": "no-path", "location": {"layerID": LAYER_C}, "metadata": {"type": "RegularFile"}},
        ],
        "source": {"type": "image", "metadata": {"userInput": "test:latest"}},
        "distro": {"prettyName": "Alpine Linux v3.22"},
        "descriptor": {"name": "syft", "version": "1.55.0"},
        "schema": {"version": "16.0.0"},
    }


@pytest.fixture
def diff_ids() -> list[str]:
    return [LAYER_A, LAYER_B, LAYER_C]


@pytest.fixture
def inspect_record(diff_ids: list[str]) -> dict[str, Any]:
    return {
        "Id": "sha256:" + "1" * 64,
        "RepoTags": ["ghcr.io/linuxserver-playground/test:latest"],
        "Architecture": "amd64",
        "Size": 1234567,
        "RootFS": {"Type": "layers", "Layers": diff_ids},
        "Config": {
            "Labels": {
                "org.opencontainers.image.version": "1.2.3-ls4",
                "io.linuxserver.image.base": "ghcr.io/linuxserver/baseimage-alpine:3.22@sha256:" + "f" * 64,
            },
            "Entrypoint": ["/init"],
        },
    }


@pytest.fixture
def base_inspect_record() -> dict[str, Any]:
    return {"Id": "sha256:" + "2" * 64, "RootFS": {"Type": "layers", "Layers": [LAYER_A, LAYER_B]}}


@pytest.fixture
def history_records() -> list[dict[str, Any]]:
    """docker history as the SDK returns it: newest first, raw byte sizes."""
    return [
        {
            "Id": "sha256:" + "1" * 64,
            "CreatedBy": 'ENTRYPOINT ["/init"]',
            "Size": 0,
            "Comment": "buildkit.dockerfile.v0",
        },
        {"Id": "<missing>", "CreatedBy": "COPY root/ / # buildkit", "Size": 4096},
        {"Id": "<missing>", "CreatedBy": "LABEL build_version=1.2.3-ls4", "Size": 0},
        {
            "Id": "<missing>",
            "CreatedBy": 'RUN /bin/sh -c echo "**** install ****" && apk add --no-cache bash # buildkit',
            "Size": 300000,
        },
        {"Id": "<missing>", "CreatedBy": "ENV PATH=/usr/bin", "Size": 0},
        {"Id": "<missing>", "CreatedBy": "COPY /root-out/ / # buildkit", "Size": 15000000},
        {"Id": "<missing>", "CreatedBy": "ARG VERSION", "Size": 0},
    ]


def write_unit(
    root: Path, name: str, kind: str, deps: Sequence[str] = (), contents: Sequence[str] = (), run: str | None = None
) -> None:
    unit = root / name
    unit.mkdir(parents=True)
    (unit / "type").write_text(kind + "\n")
    if deps:
        (unit / "dependencies.d").mkdir()
        for dep in deps:
            (unit / "dependencies.d" / dep).write_text("")
    if contents:
        (unit / "contents.d").mkdir()
        for item in contents:
            (unit / "contents.d" / item).write_text("")
    if run is not None:
        (unit / "run").write_text(run)
        (unit / "up").write_text(f"/etc/s6-overlay/s6-rc.d/{name}/run\n")


@pytest.fixture
def s6_tree(tmp_path: Path) -> Path:
    """The base image units plus one image oneshot, one image longrun, one mod unit and a stray directory."""
    root = tmp_path / "s6-rc.d"
    base = [
        ("init-envfile", "oneshot", [], "#!/usr/bin/with-contenv bash\necho envfile\n"),
        ("init-migrations", "oneshot", [], None),
        ("init-adduser", "oneshot", ["init-migrations"], None),
        ("init-device-perms", "oneshot", ["init-adduser"], None),
        ("init-os-end", "oneshot", ["init-adduser", "init-device-perms", "init-envfile"], None),
        ("init-config", "oneshot", ["init-os-end"], None),
        ("init-crontab-config", "oneshot", ["init-config"], None),
        ("init-config-end", "oneshot", ["init-config", "init-crontab-config"], None),
        ("init-mods", "oneshot", ["init-config-end"], None),
        ("init-mods-package-install", "oneshot", ["init-mods"], None),
        ("init-mods-end", "oneshot", ["init-mods-package-install"], None),
        ("init-custom-files", "oneshot", ["init-mods-end"], None),
        ("init-services", "oneshot", ["init-custom-files"], None),
        ("svc-cron", "longrun", ["init-services"], "#!/usr/bin/with-contenv bash\nexec crond\n"),
        ("ci-service-check", "oneshot", ["legacy-services"], None),
    ]
    for name, kind, deps, run in base:
        write_unit(root, name, kind, deps, run=run)
    write_unit(
        root, "init-sonarr-config", "oneshot", ["init-config"], run="#!/usr/bin/with-contenv bash\nmkdir -p /config\n"
    )
    write_unit(root, "svc-sonarr", "longrun", ["init-services"], run="#!/usr/bin/with-contenv bash\nexec sonarr\n")
    write_unit(root, "init-mod-sonarr-extra", "oneshot", ["init-config"], run="echo mod\n")
    write_unit(root, "user", "bundle", contents=["init-adduser", "svc-sonarr"])
    (root / "notaunit").mkdir()
    (root / "README").write_text("stray file\n")
    return root
