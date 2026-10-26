"""Smoke tests on the running container that hold for every image type: s6 services stay up, the declared
ports listen and the web endpoint answers without a server error."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import requests
import urllib3
from docker.models.containers import Container

from .browser import split_auth
from .checks import CheckResult
from .config import PortSpec
from .docker_client import exec_output
from .probe import TCP_LISTEN, UDP_UNCONNECTED, parse_proc_net

logger = logging.getLogger("smoke")

SERVICE_WINDOW = 15.0
"""The least time between the two service snapshots: a crash-looping service restarts within it."""
PORT_TIMEOUT = 60.0
"""How long the declared ports get to start listening after init is done."""

SERVICE_STATUS = (
    'svstat=$(command -v s6-svstat || echo /command/s6-svstat); for d in /run/service/*; do [ -d "$d" ] || continue; '
    'printf "%s " "${d##*/}"; "$svstat" -o up,wantedup,pid,updownfor "$d" 2>/dev/null || echo; done'
)


@dataclass(frozen=True)
class ServiceState:
    up: bool
    wanted_up: bool
    pid: int
    seconds: int


def parse_service_status(text: str) -> dict[str, ServiceState]:
    """`name up wantedup pid updownfor` lines into states. s6's own supervisors and unreadable lines are left out."""
    states: dict[str, ServiceState] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0].startswith("s6"):
            continue
        name, up, wanted, pid, seconds = fields
        try:
            states[name] = ServiceState(up == "true", wanted == "true", int(pid), int(seconds))
        except ValueError:
            continue
    return states


def service_status(container: Container) -> dict[str, ServiceState]:
    _code, output = exec_output(container, ["sh", "-c", SERVICE_STATUS])
    return parse_service_status(output)


def services_check(before: dict[str, ServiceState], after: dict[str, ServiceState], seconds: float) -> CheckResult:
    """FAIL when a service s6 wants up is down, or restarted between the two snapshots taken `seconds` apart."""
    if not after:
        return CheckResult("SKIP", "no s6 services found under /run/service")
    down = sorted(name for name, state in after.items() if state.wanted_up and not state.up)
    restarted = sorted(
        name
        for name, state in after.items()
        if state.up and name in before and before[name].up and before[name].pid != state.pid
    )
    problems = [f"{name} is down" for name in down] + [f"{name} restarted" for name in restarted]
    if problems:
        return CheckResult("FAIL", f"{', '.join(problems)} within {seconds:.0f}s of init")
    running = sorted(name for name, state in after.items() if state.up)
    noun = "service" if len(running) == 1 else "services"
    return CheckResult("PASS", f"{len(running)} {noun} stayed up for {seconds:.0f}s: {', '.join(running)}")


def listening(container: Container) -> set[tuple[int, str]]:
    _code, tcp = exec_output(container, ["sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"])
    _code, udp = exec_output(container, ["sh", "-c", "cat /proc/net/udp /proc/net/udp6 2>/dev/null"])
    return {(p, "tcp") for p in parse_proc_net(tcp, TCP_LISTEN)} | {
        (p, "udp") for p in parse_proc_net(udp, UDP_UNCONNECTED)
    }


def ports_check(
    container: Container,
    ports: Sequence[PortSpec],
    timeout: float = PORT_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckResult:
    """Wait for every required port in the manifest to listen. A missing one is a WARN: some only open once the
    app is configured, which a test run never does."""
    if not ports:
        return CheckResult("SKIP", "the manifest declares no required ports")
    wanted = {(p.number, p.protocol) for p in ports}
    deadline = clock() + timeout
    while True:
        missing = sorted(wanted - listening(container))
        if not missing or clock() >= deadline:
            break
        sleep(2)
    names = ", ".join(f"{port}/{proto}" for port, proto in sorted(wanted))
    if missing:
        text = f"not listening after {timeout:.0f}s: {', '.join(f'{port}/{proto}' for port, proto in missing)}"
        return CheckResult("WARN", text, warning=text)
    return CheckResult("PASS", f"listening: {names}")


def web_check(
    url: str,
    auth: str | None,
    timeout: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckResult:
    """Poll the web endpoint until it answers below 500. A page that keeps answering 5xx (nginx up, the app behind
    it down) still renders in a screenshot, so only this catches it."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    start = clock()
    deadline = start + timeout
    credentials = split_auth(auth)
    last = "no answer"
    while True:
        try:
            response = requests.get(url, timeout=10, verify=False, auth=credentials)
            if response.status_code < 500:
                return CheckResult("PASS", f"HTTP {response.status_code} from {url} after {clock() - start:.1f}s")
            last = f"HTTP {response.status_code}"
        except requests.RequestException as err:
            last = type(err).__name__
        if clock() >= deadline:
            return CheckResult("FAIL", f"{url} answered {last} for {timeout:.0f}s")
        sleep(3)
