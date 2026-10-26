from unittest.mock import MagicMock

from ci.checks import Packages, collect_sysinfo, parse_processes, read_app_logs, runtime_check

# Package tables and process listings trimmed from real fleet images (docker-*/package_versions.txt and the
# checks.PROCESSES output of a running container).
HEIMDALL_TABLE = """NAME                                VERSION           TYPE
icu-data-en                         78.1-r0           apk
icu-libs                            78.1-r0           apk
laravel/framework                   v11.44.2          php-composer
nginx                               1.28.0-r3         apk
php85-fpm                           8.5.0-r0          apk
"""
HEIMDALL_PROCESSES = (
    "nginx\t0\tnginx: master process /usr/sbin/nginx -e stderr\n"
    "nginx\t0\tnginx: worker process\n"
    "php\t0\tphp\n"
    "php-fpm85\t0\tphp-fpm: master process (/etc/php85/php-fpm.conf)\n"
    "s6-supervise\t0\t/package/admin/s6-2.13.2.0/command/s6-supervise\n"
)
SONARR_TABLE = (
    "NAME          VERSION   TYPE\nBouncyCastle.Cryptography  2.6.2  dotnet  (+5 duplicates)\nicu-libs  76.1-r0  apk\n"
)
SONARR_PROCESSES = "Sonarr\t1\t/app/sonarr/bin/Sonarr\n"
QBITTORRENT_TABLE = "NAME  VERSION  TYPE\nBencodeNET  2.3.0  dotnet\nqbittorrent  5.1.2  binary\n"
QBITTORRENT_PROCESSES = "qbittorrent-nox\t0\t/app/qbittorrent-nox\n"
HEALTHCHECKS_TABLE = "NAME  VERSION  TYPE\nDjango  5.2  python\nuwsgi  2.0.30-r0  apk\n"
HEALTHCHECKS_PROCESSES = "python3\t0\tpython3\nuwsgi\t0\t/usr/sbin/uwsgi\nuWSGIWorker1Cor\t0\t/usr/sbin/uwsgi\n"
WIKIJS_PROCESSES = "MainThread\t0\t/usr/bin/node\n"
UBOOQUITY_PROCESSES = "java\t0\tjava\n"
DONE = "2026-10-26T19:45:30.123456789Z [ls.io-init] done.\n"


def test_heimdall_is_php_not_dotnet() -> None:
    """Issue #1: ICU came in with PHP's intl and used to make heimdall a .NET application."""
    result, warnings = runtime_check(parse_processes(HEIMDALL_PROCESSES), HEIMDALL_TABLE, [DONE])
    assert result.status == "PASS" and warnings == {}
    assert result.message == "running PHP, nginx; no known runtime errors in the logs"
    assert ".NET" not in result.message


def test_dotnet_self_contained_detected_from_coreclr() -> None:
    result, _ = runtime_check(parse_processes(SONARR_PROCESSES), SONARR_TABLE, [DONE])
    assert result.status == "PASS" and result.message.startswith("running .NET;")


def test_dotnet_packages_alone_are_not_a_running_dotnet_app() -> None:
    """qbittorrent ships qbittorrent-cli (.NET) but runs qbittorrent-nox."""
    result, _ = runtime_check(parse_processes(QBITTORRENT_PROCESSES), QBITTORRENT_TABLE, [DONE])
    assert result.status == "PASS" and result.message.startswith("no recognised app runtime running;")


def test_runtimes_from_processes() -> None:
    for listing, label in ((WIKIJS_PROCESSES, "Node.js"), (UBOOQUITY_PROCESSES, "Java")):
        result, _ = runtime_check(parse_processes(listing), "", [DONE])
        assert result.message == f"running {label}; no known runtime errors in the logs"
    result, _ = runtime_check(parse_processes(HEALTHCHECKS_PROCESSES), HEALTHCHECKS_TABLE, [DONE])
    assert result.message.startswith("running Python, uWSGI;")


def test_dotnet_errors_warn() -> None:
    log = DONE + "2026-10-26T19:45:31Z Couldn't find a valid ICU package installed on the system.\n"
    result, warnings = runtime_check(parse_processes(SONARR_PROCESSES), SONARR_TABLE, [log])
    assert result.status == "WARN"
    assert warnings == {"dotnet": ".NET error in the logs: Couldn't find a valid ICU package installed on the system."}


def test_crashed_app_is_checked_through_its_packages() -> None:
    """No process is left to recognise, but the traceback of a Python app that died is still reported."""
    log = DONE + (
        "2026-10-26T19:45:31Z Traceback (most recent call last):\n"
        '2026-10-26T19:45:31Z   File "/app/bazarr/bin/bazarr.py", line 9, in <module>\n'
        "2026-10-26T19:45:31Z     import flask\n"
        "2026-10-26T19:45:31Z ModuleNotFoundError: No module named 'flask'\n"
    )
    result, warnings = runtime_check([], "NAME VERSION TYPE\nrequests 2.32 python\n", [log])
    assert result.status == "WARN" and list(warnings) == ["python"]
    assert warnings["python"] == "Python error in the logs: ModuleNotFoundError: No module named 'flask'"


def test_errors_of_absent_runtimes_are_ignored() -> None:
    """uWSGI's lines are generic ('unable to load'); they only count where uWSGI is."""
    result, warnings = runtime_check(parse_processes(HEIMDALL_PROCESSES), HEIMDALL_TABLE, ["unable to load config\n"])
    assert result.status == "PASS" and warnings == {}
    result, warnings = runtime_check(
        parse_processes(HEALTHCHECKS_PROCESSES), HEALTHCHECKS_TABLE, ["unable to load config\n"]
    )
    assert result.status == "WARN" and list(warnings) == ["uwsgi"]


def test_app_logs_are_scanned() -> None:
    app_logs = (
        "==== /config/log/nginx/error.log ====\n"
        "2026/10/26 19:45:40 [crit] 300#300: *1 connect() to unix:/var/run/php-fpm.sock failed\n"
        "==== /config/log/php/error.log ====\n[26-Oct-2026 19:45:28] NOTICE: ready to handle connections\n"
    )
    result, warnings = runtime_check(parse_processes(HEIMDALL_PROCESSES), HEIMDALL_TABLE, [DONE, app_logs])
    assert result.status == "WARN" and list(warnings) == ["nginx"]
    assert "[crit]" in warnings["nginx"]


def test_several_runtimes_with_errors() -> None:
    log = "PHP Fatal error:  Uncaught Error\n2026/10/26 [emerg] 1#1: bind() failed\n"
    result, warnings = runtime_check(parse_processes(HEIMDALL_PROCESSES), HEIMDALL_TABLE, [log])
    assert list(warnings) == ["php", "nginx"]
    assert result.message == "PHP error in the logs: PHP Fatal error:  Uncaught Error (and 1 more)"


def test_parse_processes_skips_malformed_lines() -> None:
    assert parse_processes("exec failed: boom\nnode\t0\tnode\n") == parse_processes("node\t0\tnode\n")


def test_packages_from_table() -> None:
    packages = Packages.from_table(SONARR_TABLE)
    assert "dotnet" in packages.types and "icu-libs" in packages.names
    assert "bouncycastle.cryptography" in packages.names


def test_read_app_logs_keeps_files_that_exist() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [
        MagicMock(exit_code=0, output=b"[crit] boom\n"),
        MagicMock(exit_code=1, output=b"tail: can't open"),
    ]
    assert read_app_logs(container) == "==== /config/log/nginx/error.log ====\n[crit] boom\n"


def test_collect_sysinfo_records_failures() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [MagicMock(exit_code=0, output=b"Linux x\n")] + [
        MagicMock(exit_code=127, output=b"not found")
    ] * 10
    text = collect_sysinfo(container)
    assert "==== uname -a ====\nLinux x" in text
    assert "(exit 127) not found" in text
