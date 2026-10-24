from typing import Any

import pytest

from ci.files import build_file_entries, mode_string, total_size


def by_path(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(e["path"]): e for e in entries}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(755, "0755"), (644, "0644"), (1000000777, "0777"), (40000755, "4755"), (2000000666, "0666"), (0, "0000")],
)
def test_mode_string(mode: int, expected: str) -> None:
    assert mode_string(mode) == expected


def test_entries_from_exact_hardlinks(syft_json: dict[str, Any], diff_ids: list[str]) -> None:
    links = {"/bin/coreutils": "/bin/busybox", "/usr/bin/[": "/bin/busybox"}
    entries = by_path(build_file_entries(syft_json, diff_ids, links))
    assert "no-path" not in entries and len(entries) == 12
    busybox = entries["/bin/busybox"]
    assert busybox == {
        "path": "/bin/busybox",
        "layer": 0,
        "type": "file",
        "size": 808712,
        "mode": "0755",
        "uid": 0,
        "gid": 0,
        "digest": "6d" * 8,
        "mime": "application/x-sharedlib",
        "package": "pkg:apk/alpine/busybox@1.37.0-r20",
        "executable": True,
    }
    assert entries["/bin/coreutils"]["type"] == "hardlink"
    assert entries["/bin/coreutils"]["link_target"] == "/bin/busybox"
    assert entries["/usr/bin/["]["type"] == "hardlink"
    # identical content in the same layer is NOT a hardlink when the tar says so
    assert entries["/etc/s6-overlay/s6-rc.d/init-a/type"]["type"] == "file"
    assert entries["/etc/s6-overlay/s6-rc.d/init-b/type"]["type"] == "file"
    assert entries["/bin/sh"] == {
        "path": "/bin/sh",
        "layer": 0,
        "type": "symlink",
        "size": 0,
        "mode": "0777",
        "uid": 0,
        "gid": 0,
        "link_target": "/bin/busybox",
        "package": "pkg:apk/alpine/busybox@1.37.0-r20",
    }
    assert entries["/bin"]["type"] == "dir" and "size" not in entries["/bin"]
    assert entries["/dev/null"]["type"] == "device"
    assert entries["/run/lock"]["type"] == "other"
    assert entries["/usr/bin/sudo"]["mode"] == "4755" and entries["/usr/bin/sudo"]["layer"] == 2
    assert entries["/opt/weird"]["executable"] is False and "package" not in entries["/opt/weird"]
    assert entries["/etc/s6-overlay/s6-rc.d/init-b/run"]["executable"] is True
    assert total_size(list(entries.values())) == 808712 + 8 + 8 + 120 + 1000 + 5


def test_entries_fallback_dedupes_by_layer_and_digest(syft_json: dict[str, Any], diff_ids: list[str]) -> None:
    entries = by_path(build_file_entries(syft_json, diff_ids, None))
    assert entries["/bin/busybox"]["type"] == "file"
    assert entries["/bin/coreutils"]["type"] == "hardlink"
    assert entries["/bin/coreutils"]["link_target"] == "/bin/busybox"
    assert entries["/usr/bin/["]["link_target"] == "/bin/busybox"
    assert entries["/etc/s6-overlay/s6-rc.d/init-a/type"]["type"] == "file"
    assert entries["/etc/s6-overlay/s6-rc.d/init-b/type"]["type"] == "hardlink"
    assert total_size(list(entries.values())) == 808712 + 8 + 120 + 1000 + 5


def test_unknown_layer_gets_a_new_index(syft_json: dict[str, Any]) -> None:
    entries = by_path(build_file_entries(syft_json, ["sha256:" + "a" * 64], {}))
    layers = {e["layer"] for e in entries.values()}
    assert layers == {0, 1, 2}


def test_entries_are_sorted_by_path(syft_json: dict[str, Any], diff_ids: list[str]) -> None:
    paths = [e["path"] for e in build_file_entries(syft_json, diff_ids, {})]
    assert paths == sorted(paths)
