from unittest.mock import MagicMock

from ci.checks import collect_sysinfo, dotnet_check, uwsgi_check

ALPINE_TABLE = "NAME  VERSION  TYPE\nbash  5.2  apk\nicu-libs  74.2-r0  apk\n"
UBUNTU_TABLE = "NAME  VERSION  TYPE\nbash  5.2  deb\nuwsgi  2.0.22  deb\n"


def test_dotnet_not_detected() -> None:
    result = dotnet_check("NAME VERSION\nbash 5.2\n", "[ls.io-init] done.")
    assert result.status == "PASS" and result.warning is None


def test_dotnet_detected_without_errors() -> None:
    result = dotnet_check(ALPINE_TABLE, "[ls.io-init] done.\nSonarr started")
    assert result.status == "PASS"
    assert ".NET" in result.message


def test_dotnet_detected_with_errors_warns() -> None:
    log = "[ls.io-init] done.\nCouldn't find a valid ICU package installed on the system.\n"
    result = dotnet_check(ALPINE_TABLE, log)
    assert result.status == "WARN"
    assert result.warning is not None and "ICU" in result.warning


def test_uwsgi() -> None:
    assert uwsgi_check(ALPINE_TABLE, "").status == "PASS"
    assert uwsgi_check(UBUNTU_TABLE, "[uwsgi] ready").status == "PASS"
    result = uwsgi_check(UBUNTU_TABLE, "probably another instance of uWSGI is running on the same address")
    assert result.status == "WARN" and result.warning


def test_collect_sysinfo_records_failures() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [MagicMock(exit_code=0, output=b"Linux x\n")] + [
        MagicMock(exit_code=127, output=b"not found")
    ] * 10
    text = collect_sysinfo(container)
    assert "==== uname -a ====\nLinux x" in text
    assert "(exit 127) not found" in text
