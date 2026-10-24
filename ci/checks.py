"""Cheap checks on the running container: dotnet and uwsgi sanity, system information."""

from __future__ import annotations

from dataclasses import dataclass

from docker.models.containers import Container

from .docker_client import exec_output

DOTNET_MARKERS = ("icu-libs", "libicu", "dotnet", "aspnetcore")
DOTNET_ERRORS = (
    "Couldn't find a valid ICU package",
    "Failed to create CoreCLR",
    "Unhandled exception.",
    "Could not load file or assembly",
    "No usable version of libssl",
)
UWSGI_ERRORS = (
    "unable to load",
    "unable to find",
    "no python application found",
    "probably another instance of uWSGI is running",
    "bind(): Address already in use",
)


@dataclass(frozen=True)
class CheckResult:
    status: str  # PASS, WARN, FAIL or SKIP
    message: str
    warning: str | None = None


def _mentions(text: str, markers: tuple[str, ...]) -> list[str]:
    lower = text.lower()
    return [m for m in markers if m.lower() in lower]


def dotnet_check(package_table: str, container_log: str) -> CheckResult:
    """Images that ship a .NET runtime fail in recognisable ways; surface those lines instead of a bare startup pass."""
    if not _mentions(package_table, DOTNET_MARKERS) and "dotnet" not in container_log.lower():
        return CheckResult("PASS", "not a .NET application")
    hits = [line for line in container_log.splitlines() if _mentions(line, DOTNET_ERRORS)]
    if hits:
        text = f".NET runtime errors in the container log: {hits[0].strip()[:200]}"
        return CheckResult("WARN", text, warning=text)
    return CheckResult("PASS", ".NET application detected, no runtime errors in the log")


def uwsgi_check(package_table: str, container_log: str) -> CheckResult:
    if "uwsgi" not in package_table.lower():
        return CheckResult("PASS", "image does not use uWSGI")
    hits = [line for line in container_log.splitlines() if _mentions(line, UWSGI_ERRORS)]
    if hits:
        text = f"uWSGI errors in the container log: {hits[0].strip()[:200]}"
        return CheckResult("WARN", text, warning=text)
    return CheckResult("PASS", "uWSGI application detected, no startup errors in the log")


SYSINFO_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("uname -a", ["uname", "-a"]),
    ("/etc/os-release", ["cat", "/etc/os-release"]),
    ("id abc", ["id", "abc"]),
    ("processes", ["ps", "-o", "pid,user,rss,args"]),
    ("mounts", ["sh", "-c", "cat /proc/mounts | cut -d' ' -f1-4"]),
    ("s6-rc units", ["ls", "/etc/s6-overlay/s6-rc.d"]),
)


def collect_sysinfo(container: Container) -> str:
    """A plain text dump of a few commands run inside the container, each failure recorded instead of raised."""
    sections: list[str] = []
    for title, command in SYSINFO_COMMANDS:
        code, output = exec_output(container, command)
        body = output.strip() or "(no output)"
        if code != 0:
            body = f"(exit {code}) {body}"
        sections.append(f"==== {title} ====\n{body}\n")
    return "\n".join(sections)
