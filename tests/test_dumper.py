"""Running dumper inside a created container, with the docker SDK mocked out."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ci import dumper


def archive(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "dumper"
    path.write_bytes(b"\x7fELF fake")
    monkeypatch.setenv("DUMPER", str(path))
    return path


def client_with(container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.containers.create.return_value = container
    return client


def test_file_tree_copies_in_runs_and_copies_out(tmp_path: Path, binary: Path) -> None:
    container = MagicMock()
    container.put_archive.return_value = True
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b"dumper files: 3 dirs, 2 files (10 bytes); walk 0.01s, total 0.02s\n"
    container.get_archive.return_value = (iter([archive("files.json.gz", b"gzipped tree")]), {})
    dest = tmp_path / "files.amd64.json.gz"

    summary = dumper.file_tree(client_with(container), "ghcr.io/x/y:latest", dest)

    assert summary == "3 dirs, 2 files (10 bytes); walk 0.01s, total 0.02s"
    assert dest.read_bytes() == b"gzipped tree"
    path, data = container.put_archive.call_args.args
    assert path == "/"
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.getmember(".lsio-dumper/dumper")
        assert member.mode == 0o755
        fh = tar.extractfile(member)
        assert fh is not None and fh.read() == b"\x7fELF fake"
    container.get_archive.assert_called_once_with("/.lsio-dumper/files.json.gz")
    container.remove.assert_called()


def test_container_is_created_without_init_or_network(tmp_path: Path, binary: Path) -> None:
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b""
    container.get_archive.return_value = (iter([archive("files.json.gz", b"x")]), {})
    client = client_with(container)
    dumper.file_tree(client, "img", tmp_path / "out.gz")
    args, kwargs = client.containers.create.call_args
    assert args == ("img",)
    assert kwargs["entrypoint"] == ["/.lsio-dumper/dumper"]
    assert kwargs["command"] == ["files", "-o", "/.lsio-dumper/files.json.gz", "--exclude", "/.lsio-dumper"]
    assert kwargs["network_disabled"] is True and kwargs["user"] == "0:0"


def test_nonzero_exit_is_an_error_and_the_container_is_removed(tmp_path: Path, binary: Path) -> None:
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 2}
    container.logs.return_value = b"unknown argument"
    with pytest.raises(dumper.DumperError, match="exited 2: unknown argument"):
        dumper.file_tree(client_with(container), "img", tmp_path / "out.gz")
    container.remove.assert_called()


def test_missing_binary_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DUMPER", str(tmp_path / "nope"))
    with pytest.raises(dumper.DumperError, match="not found"):
        dumper.dumper_binary()
