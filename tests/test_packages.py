from typing import Any

import pytest
import requests

from ci.packages import diff_text, package_count, package_diff, parse_table

OLD = """NAME                    VERSION           TYPE
alpine-baselayout       3.7.0-r0          apk
bash                    5.2.37-r0         apk
curl                    8.14.1-r1         apk
Simple Launcher         1.1.0.14          binary     (+5 duplicates)
"""
NEW = """NAME                    VERSION           TYPE
alpine-baselayout       3.7.0-r0          apk
bash                    5.2.37-r1         apk
jq                      1.8.0-r0          apk
"""


def test_parse_table() -> None:
    assert parse_table(OLD) == {
        "alpine-baselayout": "3.7.0-r0",
        "bash": "5.2.37-r0",
        "curl": "8.14.1-r1",
        "Simple": "Launcher",
    }
    assert parse_table("") == {}


def test_diff_text() -> None:
    text = diff_text(parse_table(OLD), parse_table(NEW))
    assert text.splitlines() == [
        "[-] Simple: Launcher (Removed)",
        "[*] bash: 5.2.37-r0 -> 5.2.37-r1 (Changed)",
        "[-] curl: 8.14.1-r1 (Removed)",
        "[+] jq: 1.8.0-r0 (Added)",
    ]
    assert diff_text(parse_table(OLD), parse_table(OLD)) == "No package changes found."


def test_package_diff_fetches_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200
        ok = True
        text = OLD

    monkeypatch.setattr(requests, "get", lambda url, timeout: Response())
    assert "[+] jq" in package_diff("https://example.invalid/p.txt", NEW)
    assert package_diff(None, NEW) == "No previous release to compare against."


def test_package_diff_handles_missing_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 404
        ok = False
        text = ""

    monkeypatch.setattr(requests, "get", lambda url, timeout: Response())
    assert package_diff("https://example.invalid/p.txt", NEW).startswith("Previous package list unavailable")

    def boom(url: str, timeout: int) -> Any:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert "unavailable" in package_diff("https://example.invalid/p.txt", NEW)


def test_package_count(syft_json: dict[str, Any]) -> None:
    assert package_count(syft_json) == 2
    assert package_count({}) == 0
