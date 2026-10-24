import tarfile
from datetime import UTC, datetime
from pathlib import Path

from contracts import validate

from ci import report as report_module
from ci.report import Report, render_html, rfc3339, validate_report, write_tarball

TestResult = report_module.TestResult
TestResult.__test__ = False  # type: ignore[attr-defined]


def sample_report() -> Report:
    report = Report(
        image="sonarr", tag="amd64-4.0.20-ls1", arch="amd64", started=datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    )
    report.finished = datetime(2026, 10, 24, 12, 3, 30, 123456, tzinfo=UTC)
    report.engine = "29.9.2"
    report.add(TestResult("Container startup", "PASS", "init done after 4.2s", 4.2))
    report.add(TestResult("Dotnet check", "WARN", "errors", None))
    report.add(TestResult("Take screenshot", "SKIP", "manifest has no test.web"))
    report.warnings = {"dotnet": "errors"}
    report.startup_seconds = 4.2
    report.runtime = {
        "rss_bytes": 100,
        "pids": 3,
        "listening_tcp": [8989],
        "listening_udp": [],
        "s6_services": ["svc-sonarr"],
        "changed_paths": 10,
    }
    report.files = {"html": "index.html", "container_log": "container.log"}
    return report


def test_report_validates_against_the_contract() -> None:
    record = sample_report().as_dict()
    validate("test-report", record)
    assert validate_report(record) == []
    assert record["success"] is True
    assert record["started"] == "2026-10-24T12:00:00Z" and record["finished"] == "2026-10-24T12:03:30Z"
    assert record["tests"][1] == {"name": "Dotnet check", "status": "WARN", "message": "errors"}


def test_failure_and_empty_reports() -> None:
    report = sample_report()
    report.add(TestResult("Layers", "FAIL", "boom", 0.1))
    assert report.success is False
    assert validate_report(report.as_dict()) == []
    empty = Report(image="x", tag="t", arch="arm64", started=datetime.now(UTC))
    assert empty.success is False  # nothing passed, nothing to celebrate
    assert validate_report(empty.as_dict()) == []


def test_validate_report_names_violations() -> None:
    record = sample_report().as_dict()
    record["tests"][0]["status"] = "MAYBE"
    record["bogus"] = 1
    problems = validate_report(record)
    assert any("MAYBE" in p for p in problems) and any("bogus" in p for p in problems)


def test_rfc3339() -> None:
    assert rfc3339(datetime(2026, 1, 2, 3, 4, 5, 678, tzinfo=UTC)) == "2026-01-02T03:04:05Z"


def test_write_tarball_and_html(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "container.log").write_text("log\n")
    (work / "sysinfo.txt").write_text("info\n")
    (work / "package_diff.txt").write_text("diff\n")
    html = render_html(
        {
            "image": "ghcr.io/linuxserver-playground/sonarr:latest",
            "name": "sonarr",
            "tag": "latest",
            "arch": "amd64",
            "success": False,
            "tests": [t.as_dict() for t in sample_report().tests],
            "warnings": {"dotnet": "<b>bold</b>"},
            "runtime": sample_report().runtime,
            "build_info": {"version": "1"},
            "container_log": "<script>alert(1)</script>",
            "package_table": "NAME VERSION",
            "package_diff": "diff",
            "browser_log": "",
            "sysinfo": "info",
            "screenshot": False,
            "startup_seconds": 4.2,
            "total_runtime": "10.0s",
            "ci_version": "2.0.0",
        }
    )
    (work / "index.html").write_text(html)
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert "&lt;b&gt;bold" in html
    assert "FAIL" in html and "Dotnet check" in html

    files = write_tarball(work, tmp_path / "test.amd64.tar.gz")
    assert files == {"html": "index.html", "container_log": "container.log", "sysinfo": "sysinfo.txt"}
    with tarfile.open(tmp_path / "test.amd64.tar.gz") as tar:
        assert sorted(tar.getnames()) == ["container.log", "index.html", "package_diff.txt", "sysinfo.txt"]
