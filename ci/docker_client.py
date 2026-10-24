"""A thin layer over the docker SDK so the rest of the harness is easy to mock."""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path
from typing import Any

import docker
from docker import DockerClient
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

logger = logging.getLogger("docker")


class DockerUnavailable(RuntimeError):
    """The daemon cannot be reached or the image is not present."""


def connect() -> DockerClient:
    try:
        client = docker.from_env(timeout=120)
        client.ping()
    except DockerException as err:
        raise DockerUnavailable(f"cannot talk to the Docker daemon: {err}") from err
    return client


def engine_version(client: DockerClient) -> str:
    try:
        version: dict[str, Any] = client.version()
        return str(version.get("Version", "unknown"))
    except DockerException:
        return "unknown"


def inspect_image(client: DockerClient, ref: str) -> dict[str, Any]:
    """`docker image inspect` as one dict. Raises DockerUnavailable when the image is missing."""
    try:
        attrs: dict[str, Any] = client.images.get(ref).attrs
    except ImageNotFound as err:
        raise DockerUnavailable(f"image {ref} is not present in the local Docker daemon") from err
    except DockerException as err:
        raise DockerUnavailable(f"cannot inspect {ref}: {err}") from err
    return attrs


def try_inspect_image(client: DockerClient, ref: str) -> dict[str, Any] | None:
    try:
        attrs: dict[str, Any] = client.images.get(ref).attrs
        return attrs
    except DockerException:
        return None


def image_history(client: DockerClient, ref: str) -> list[dict[str, Any]]:
    """`docker history` with raw byte sizes, newest layer first (the API order)."""
    history: list[dict[str, Any]] = client.api.history(ref)  # type: ignore[no-untyped-call]
    return history


def local_image_tags(client: DockerClient) -> dict[str, list[str]]:
    """{image id: [repo tags]} for every image in the daemon."""
    out: dict[str, list[str]] = {}
    for image in client.images.list():
        tags = list(image.attrs.get("RepoTags") or [])
        if tags:
            out[str(image.id)] = tags
    return out


def run_container(client: DockerClient, ref: str, env: dict[str, str]) -> Container:
    container: Container = client.containers.run(ref, detach=True, shm_size="1G", environment=env)
    return container


def container_logs(container: Container, timestamps: bool = False) -> str:
    try:
        raw: bytes = container.logs(timestamps=timestamps)
        return raw.decode("utf-8", errors="replace")
    except DockerException as err:
        logger.warning("could not read container logs: %s", err)
        return ""


def container_running(container: Container) -> bool:
    try:
        container.reload()
    except DockerException:
        return False
    return str(container.status) == "running"


def container_ip(container: Container) -> str:
    container.reload()
    networks: dict[str, Any] = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for network in networks.values():
        ip = str(network.get("IPAddress", ""))
        if ip:
            return ip
    return ""


def exec_output(container: Container, command: list[str], timeout_note: str = "") -> tuple[int, str]:
    """Run a command inside the container and return (exit code, stdout+stderr)."""
    try:
        result = container.exec_run(command, demux=False)
    except DockerException as err:
        return 1, f"exec failed: {err}"
    raw = result.output
    output: bytes = raw if isinstance(raw, bytes) else b"".join(raw) if raw else b""
    return int(result.exit_code if result.exit_code is not None else 1), output.decode("utf-8", errors="replace")


def changed_path_count(container: Container) -> int | None:
    try:
        changes: list[dict[str, Any]] | None = container.diff()
    except DockerException:
        return None
    return len(changes or [])


def remove_container(container: Container) -> None:
    try:
        container.remove(force=True)
    except (NotFound, APIError) as err:
        logger.warning("could not remove container %s: %s", container.short_id, err)


def copy_from_image(client: DockerClient, ref: str, src: str, dest: Path) -> bool:
    """Copy a directory out of an image without starting it (docker create + docker cp).

    Returns False when the path does not exist in the image."""
    container = None
    try:
        container = client.containers.create(ref)
        stream, _stat = container.get_archive(src)
        data = io.BytesIO(b"".join(stream))
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=data) as tar:
            tar.extractall(dest, filter="data")
        return True
    except NotFound:
        return False
    except (DockerException, tarfile.TarError, OSError) as err:
        logger.warning("could not copy %s from %s: %s", src, ref, err)
        return False
    finally:
        if container is not None:
            remove_container(container)
