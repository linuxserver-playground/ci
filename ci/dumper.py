"""dumper: the static binary from linuxserver-playground/dumper, run inside the image for the file tree.

The workflow fetches the latest release and mounts it into this container (DUMPER, default `dumper` on PATH).
It is copied into a created, never-started container of the image and run as the entrypoint, so init never
runs and the walk sees the image as shipped. Nothing on the host is mounted, so this works from inside the ci
container too."""

from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
from pathlib import Path

from docker import DockerClient
from docker.errors import DockerException

from .docker_client import remove_container

logger = logging.getLogger("dumper")

DIR = "/.lsio-dumper"
"""Where the binary and its output live inside the container; left out of the tree."""


class DumperError(RuntimeError):
    pass


def dumper_binary() -> Path:
    configured = os.environ.get("DUMPER", "").strip()
    path = configured or shutil.which("dumper")
    if not path or not Path(path).is_file():
        raise DumperError(f"dumper binary not found ({'DUMPER=' + configured if configured else 'not on PATH'})")
    return Path(path)


def _binary_tar(binary: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        folder = tarfile.TarInfo(DIR.lstrip("/"))
        folder.type, folder.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(folder)
        info = tarfile.TarInfo(f"{DIR.lstrip('/')}/dumper")
        info.size, info.mode = binary.stat().st_size, 0o755
        with binary.open("rb") as fh:
            tar.addfile(info, fh)
    return buf.getvalue()


def _read_file(chunks: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(chunks)) as tar:
        member = tar.getmember(name)
        fh = tar.extractfile(member)
        if fh is None:
            raise DumperError(f"{name} is not a regular file in the archive")
        return fh.read()


def file_tree(client: DockerClient, image: str, dest: Path, timeout: int = 600) -> str:
    """Write `dumper files` output for `image` to `dest`; returns dumper's summary line."""
    binary = dumper_binary()
    output = f"{DIR}/files.json.gz"
    container = None
    try:
        container = client.containers.create(
            image,
            entrypoint=[f"{DIR}/dumper"],
            command=["files", "-o", output, "--exclude", DIR],
            user="0:0",
            network_disabled=True,
        )
        if not container.put_archive("/", _binary_tar(binary)):
            raise DumperError("could not copy dumper into the container")
        container.start()
        status = container.wait(timeout=timeout)
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace").strip()
        code = int(status.get("StatusCode", -1))
        if code != 0:
            raise DumperError(f"dumper exited {code}: {stderr[-2000:]}")
        stream, _stat = container.get_archive(output)
        dest.write_bytes(_read_file(b"".join(stream), Path(output).name))
    except DockerException as err:
        raise DumperError(f"{type(err).__name__}: {err}") from err
    finally:
        if container is not None:
            remove_container(container)
    summary = stderr.splitlines()[-1] if stderr else "no summary"
    logger.info("%s", summary)
    return summary.removeprefix("dumper files: ")
