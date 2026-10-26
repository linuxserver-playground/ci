"""Cheap checks on the running container: which app runtimes run and their known failure lines, system information."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from docker.models.containers import Container

from .docker_client import exec_output

PROCESSES = (
    'for p in /proc/[0-9]*; do a0=$(tr "\\0" "\\n" < "$p/cmdline" 2>/dev/null | head -n 1); [ -n "$a0" ] || continue; '
    'case "$a0" in /*) a0=$(readlink -f "$a0" 2>/dev/null || echo "$a0");; esac; '
    'clr=0; [ -e "${a0%/*}/libcoreclr.so" ] && clr=1; '
    'printf "%s\\t%s\\t%s\\n" "$(cat "$p/comm" 2>/dev/null)" "$clr" "$a0"; done'
)
"""One `comm<TAB>coreclr<TAB>argv0` line per process. /proc/<pid>/exe and maps are out of reach (exec has no
CAP_SYS_PTRACE and the app runs as abc), but comm and cmdline are world-readable. A self-contained .NET app has
libcoreclr.so next to its binary."""

APP_LOGS = ("/config/log/nginx/error.log", "/config/log/php/error.log")
"""Logs that never reach the container log: the nginx bases send nginx and php-fpm errors here."""

TRACEBACK = "Traceback (most recent call last):"
DOCKER_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\S+Z ")


@dataclass(frozen=True)
class CheckResult:
    status: str  # PASS, WARN, FAIL or SKIP
    message: str
    warning: str | None = None


@dataclass(frozen=True)
class Process:
    comm: str
    argv0: str
    coreclr: bool = False

    @property
    def program(self) -> str:
        return self.argv0.split(" ", 1)[0].rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Packages:
    """The syft table reduced to what runtime detection asks: package names and package types."""

    names: frozenset[str]
    types: frozenset[str]

    @classmethod
    def from_table(cls, table: str) -> Packages:
        names: set[str] = set()
        types: set[str] = set()
        for line in table.lower().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                names.add(fields[0])
                types.update(fields[1:])
        return cls(frozenset(names), frozenset(types))


@dataclass(frozen=True)
class Runtime:
    key: str
    label: str
    errors: tuple[str, ...]
    runs: Callable[[Process], bool]
    installed: Callable[[Packages], bool]


RUNTIMES: tuple[Runtime, ...] = (
    Runtime(
        "dotnet",
        ".NET",
        (
            "Couldn't find a valid ICU package",
            "Failed to create CoreCLR",
            "Unhandled exception.",
            "Could not load file or assembly",
            "No usable version of libssl",
        ),
        lambda p: p.coreclr or p.program == "dotnet",
        lambda pk: "dotnet" in pk.types,
    ),
    Runtime(
        "java",
        "Java",
        (
            'Exception in thread "main"',
            "Error: Could not find or load main class",
            "Error: Unable to access jarfile",
            "UnsupportedClassVersionError",
            "java.lang.OutOfMemoryError",
            "Error occurred during initialization of VM",
        ),
        lambda p: p.program == "java" or p.comm == "java",
        lambda pk: "java-archive" in pk.types,
    ),
    Runtime(
        "node",
        "Node.js",
        ("Error: Cannot find module", "ERR_MODULE_NOT_FOUND", "UnhandledPromiseRejection"),
        lambda p: p.program in ("node", "nodejs"),
        lambda pk: "npm" in pk.types,
    ),
    Runtime(
        "python",
        "Python",
        (TRACEBACK, "ModuleNotFoundError: "),
        lambda p: p.program.startswith("python"),
        lambda pk: "python" in pk.types,
    ),
    Runtime(
        "php",
        "PHP",
        ("PHP Fatal error", "PHP Parse error", "PHP Startup: Unable to load dynamic library", "] ERROR: ", "] ALERT: "),
        lambda p: p.comm.startswith("php"),
        lambda pk: "php-composer" in pk.types or any(n.startswith("php") for n in pk.names),
    ),
    Runtime(
        "nginx",
        "nginx",
        ("[emerg]", "[alert]", "[crit]"),
        lambda p: p.comm == "nginx",
        lambda pk: "nginx" in pk.names,
    ),
    Runtime(
        "uwsgi",
        "uWSGI",
        (
            "unable to load",
            "unable to find",
            "no python application found",
            "probably another instance of uWSGI is running",
            "bind(): Address already in use",
        ),
        lambda p: p.comm == "uwsgi" or p.comm.startswith("uWSGI"),
        lambda pk: "uwsgi" in pk.names,
    ),
)


def parse_processes(text: str) -> list[Process]:
    processes: list[Process] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            processes.append(Process(fields[0], fields[2], fields[1] == "1"))
    return processes


def list_processes(container: Container) -> list[Process]:
    _code, output = exec_output(container, ["sh", "-c", PROCESSES])
    return parse_processes(output)


def read_app_logs(container: Container) -> str:
    """The last lines of each of APP_LOGS that exists, under a header per file."""
    sections: list[str] = []
    for path in APP_LOGS:
        code, output = exec_output(container, ["tail", "-n", "500", path])
        if code == 0 and output.strip():
            sections.append(f"==== {path} ====\n{output.rstrip()}\n")
    return "\n".join(sections)


def _failure_line(lines: Sequence[str], index: int) -> str:
    """The line that says what went wrong: for a Python traceback the exception after the indented frames."""
    line = lines[index]
    if TRACEBACK in line:
        for following in lines[index + 1 :]:
            if following and not following[0].isspace():
                return following.strip()
    return line.strip()


def runtime_check(
    processes: Sequence[Process], package_table: str, logs: Sequence[str]
) -> tuple[CheckResult, dict[str, str]]:
    """Name the runtimes the container ran, and look for each one's known failure lines in the logs.

    A runtime's lines are looked for when one of its processes ran or its packages are installed: an app that
    crashed before anyone looked has no process left, and its traceback is the useful part. A hit is a WARN,
    with one entry per runtime for the report's `warnings`."""
    packages = Packages.from_table(package_table)
    running = [r for r in RUNTIMES if any(r.runs(p) for p in processes)]
    candidates = [r for r in RUNTIMES if r in running or r.installed(packages)]
    lines = [DOCKER_TIMESTAMP.sub("", line).rstrip() for text in logs for line in text.splitlines()]
    warnings: dict[str, str] = {}
    for runtime in candidates:
        hit = next((i for i, line in enumerate(lines) if any(e in line for e in runtime.errors)), None)
        if hit is not None:
            warnings[runtime.key] = f"{runtime.label} error in the logs: {_failure_line(lines, hit)[:200]}"
    names = f"running {', '.join(r.label for r in running)}" if running else "no recognised app runtime running"
    if warnings:
        first = next(iter(warnings.values()))
        more = f" (and {len(warnings) - 1} more)" if len(warnings) > 1 else ""
        return CheckResult("WARN", f"{first}{more}", warning=first), warnings
    return CheckResult("PASS", f"{names}; no known runtime errors in the logs"), warnings


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
