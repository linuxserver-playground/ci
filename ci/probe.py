"""Runtime observations of the idle container: memory, processes, listening sockets, services, changed paths."""

from __future__ import annotations

import logging
from typing import Any

from docker.models.containers import Container

from .docker_client import changed_path_count, exec_output

logger = logging.getLogger("probe")

TCP_LISTEN = "0A"
UDP_UNCONNECTED = "07"


def parse_proc_net(text: str, state: str) -> set[int]:
    """Local ports from a /proc/net/{tcp,tcp6,udp,udp6} dump whose connection state matches `state`."""
    ports: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3].upper() != state:
            continue
        local = fields[1]
        try:
            ports.add(int(local.rsplit(":", 1)[1], 16))
        except (IndexError, ValueError):
            continue
    return ports


def parse_rss(text: str) -> int:
    """Sum of VmRSS lines (kB) into bytes."""
    total = 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                total += int(parts[1]) * 1024
    return total


def _read(container: Container, script: str) -> str:
    code, output = exec_output(container, ["sh", "-c", script])
    if code != 0:
        logger.debug("probe command failed (%s): %s", code, script)
    return output


def probe(container: Container) -> dict[str, Any]:
    """Collect the `runtime` block of the test report. Every observation is optional."""
    runtime: dict[str, Any] = {}
    status = _read(container, "for p in /proc/[0-9]*; do grep VmRSS $p/status 2>/dev/null; done")
    if status.strip():
        runtime["rss_bytes"] = parse_rss(status)
    pids = _read(container, "ls -d /proc/[0-9]* 2>/dev/null | wc -l").strip()
    if pids.isdigit():
        runtime["pids"] = int(pids)
    tcp = parse_proc_net(_read(container, "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"), TCP_LISTEN)
    udp = parse_proc_net(_read(container, "cat /proc/net/udp /proc/net/udp6 2>/dev/null"), UDP_UNCONNECTED)
    runtime["listening_tcp"] = sorted(tcp)
    runtime["listening_udp"] = sorted(udp)
    services = _read(
        container,
        'for d in /etc/s6-overlay/s6-rc.d/*/; do [ "$(cat "$d/type" 2>/dev/null)" = longrun ] && basename "$d"; done',
    )
    runtime["s6_services"] = sorted(s for s in services.split() if s)
    changed = changed_path_count(container)
    if changed is not None:
        runtime["changed_paths"] = changed
    return runtime
