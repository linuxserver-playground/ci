"""Wait for the s6 init to report done."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from docker.models.containers import Container

from .docker_client import container_logs, container_running

logger = logging.getLogger("startup")

DONE_MARKERS = ("[ls.io-init] done.", "[services.d] done.")


@dataclass(frozen=True)
class StartupResult:
    success: bool
    seconds: float
    message: str


def init_done(log: str) -> bool:
    return any(marker in log for marker in DONE_MARKERS)


def wait_for_init(
    container: Container,
    timeout: float,
    poll: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StartupResult:
    """Tail the container log until the init done marker appears, the container exits, or `timeout` passes."""
    start = clock()
    deadline = start + timeout
    logger.info("waiting up to %ss for the init done marker", timeout)
    while True:
        log = container_logs(container)
        if init_done(log):
            seconds = clock() - start
            return StartupResult(True, seconds, f"init done after {seconds:.1f}s")
        if not container_running(container):
            return StartupResult(False, clock() - start, "container exited before init finished")
        if clock() >= deadline:
            return StartupResult(False, clock() - start, f"init did not finish within {timeout:.0f}s")
        sleep(poll)
