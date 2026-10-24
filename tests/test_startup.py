from unittest.mock import MagicMock

from ci.startup import init_done, wait_for_init


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def container_with_logs(*logs: str, status: str = "running") -> MagicMock:
    container = MagicMock()
    container.logs.side_effect = [log.encode() for log in logs] + [logs[-1].encode()] * 1000
    container.status = status
    return container


def test_init_done_markers() -> None:
    assert init_done("[migrations] started\n[ls.io-init] done.\n")
    assert init_done("[services.d] done.")
    assert not init_done("[ls.io-init] starting")


def test_wait_for_init_succeeds_when_marker_appears() -> None:
    clock = FakeClock()
    container = container_with_logs("[migrations] started", "[migrations] done", "stuff\n[ls.io-init] done.\n")
    result = wait_for_init(container, timeout=120, clock=clock, sleep=clock.sleep)
    assert result.success
    assert result.seconds == 2.0
    assert "init done" in result.message


def test_wait_for_init_times_out() -> None:
    clock = FakeClock()
    container = container_with_logs("still starting")
    result = wait_for_init(container, timeout=10, clock=clock, sleep=clock.sleep)
    assert not result.success
    assert "within 10s" in result.message
    assert clock.now >= 10


def test_wait_for_init_detects_exit() -> None:
    clock = FakeClock()
    container = container_with_logs("crash", status="exited")
    result = wait_for_init(container, timeout=120, clock=clock, sleep=clock.sleep)
    assert not result.success
    assert "exited" in result.message
    assert clock.now == 0.0
