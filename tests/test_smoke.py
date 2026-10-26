from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from ci.config import PortSpec
from ci.smoke import ServiceState, parse_service_status, ports_check, services_check, web_check
from tests.test_probe import TCP, UDP

# smoke.SERVICE_STATUS output from a running heimdall.
HEIMDALL_SERVICES = """s6-linux-init-shutdownd true true 18 30
s6rc-fdholder false false -1 30
s6rc-oneshot-runner true true 49 30
svc-cron true true 286 25
svc-nginx true true 287 25
svc-php-fpm true true 284 25
svc-queue true true 285 25
"""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_parse_service_status_leaves_out_s6_internals() -> None:
    states = parse_service_status(HEIMDALL_SERVICES + "svc-broken\n")
    assert sorted(states) == ["svc-cron", "svc-nginx", "svc-php-fpm", "svc-queue"]
    assert states["svc-nginx"] == ServiceState(up=True, wanted_up=True, pid=287, seconds=25)


def test_services_stay_up() -> None:
    states = parse_service_status(HEIMDALL_SERVICES)
    result = services_check(states, states, 15.2)
    assert result.status == "PASS"
    assert result.message == "4 services stayed up for 15s: svc-cron, svc-nginx, svc-php-fpm, svc-queue"


def test_service_down_or_restarted_fails() -> None:
    before = {"svc-app": ServiceState(True, True, 100, 1), "svc-web": ServiceState(True, True, 200, 1)}
    after = {"svc-app": ServiceState(False, True, -1, 0), "svc-web": ServiceState(True, True, 230, 2)}
    result = services_check(before, after, 20)
    assert result.status == "FAIL"
    assert result.message == "svc-app is down, svc-web restarted within 20s of init"


def test_service_held_down_on_purpose_passes() -> None:
    """A unit s6 does not want up (s6-svc -d, or a run-once service) is not a crash."""
    after = {"svc-optional": ServiceState(False, False, -1, 10), "svc-app": ServiceState(True, True, 5, 10)}
    assert services_check(after, after, 15).status == "PASS"


def test_no_services_skips() -> None:
    assert services_check({}, {}, 15).status == "SKIP"


def proc_net(container: MagicMock, *rounds: tuple[str, str]) -> None:
    outputs: list[Any] = []
    for tcp, udp in rounds:
        outputs += [MagicMock(exit_code=0, output=tcp.encode()), MagicMock(exit_code=0, output=udp.encode())]
    container.exec_run.side_effect = outputs


def test_ports_listening() -> None:
    container = MagicMock()
    proc_net(container, (TCP, UDP))
    ports = [PortSpec(internal="8989"), PortSpec(internal="53/udp")]
    result = ports_check(container, ports)
    assert result.status == "PASS" and result.message == "listening: 53/udp, 8989/tcp"


def test_ports_waits_for_late_listeners() -> None:
    container = MagicMock()
    proc_net(container, ("", ""), (TCP, ""))
    clock = Clock()
    result = ports_check(container, [PortSpec(internal="8989")], clock=clock, sleep=clock.sleep)
    assert result.status == "PASS" and clock.now == 2


def test_ports_missing_warns() -> None:
    container = MagicMock()
    container.exec_run.return_value = MagicMock(exit_code=0, output=TCP.encode())
    clock = Clock()
    result = ports_check(container, [PortSpec(internal="8989"), PortSpec(internal="8920")], 10, clock, clock.sleep)
    assert result.status == "WARN" and result.warning == "not listening after 10s: 8920/tcp"


def test_ports_none_declared_skips() -> None:
    assert ports_check(MagicMock(), []).status == "SKIP"


def response(status: int) -> MagicMock:
    return MagicMock(status_code=status)


def test_web_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([requests.ConnectionError("refused"), response(502), response(200)])

    def get(url: str, **kwargs: Any) -> Any:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(requests, "get", get)
    clock = Clock()
    result = web_check("http://172.17.0.2:80/", None, 60, clock, clock.sleep)
    assert result.status == "PASS" and result.message == "HTTP 200 from http://172.17.0.2:80/ after 6.0s"


def test_web_client_errors_still_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login wall or a 404 at / is an app that is up."""
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: response(401))
    assert web_check("http://x/", None, 60).status == "PASS"


def test_web_server_errors_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def get(url: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return response(502)

    monkeypatch.setattr(requests, "get", get)
    clock = Clock()
    result = web_check("https://x:3001/", "abc:secret", 9, clock, clock.sleep)
    assert result.status == "FAIL" and result.message == "https://x:3001/ answered HTTP 502 for 9s"
    assert calls[0]["auth"] == ("abc", "secret") and calls[0]["verify"] is False
