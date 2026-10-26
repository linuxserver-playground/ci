"""The run: start the container, observe it, generate the bundle assets, write the report."""

from __future__ import annotations

import logging
import platform
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from docker import DockerClient
from docker.errors import DockerException
from docker.models.containers import Container

from . import browser, checks, dumper, initgraph, layers, packages, probe, smoke, startup
from .config import Settings
from .docker_client import (
    connect,
    container_ip,
    container_logs,
    container_running,
    copy_from_image,
    engine_version,
    image_history,
    inspect_image,
    remove_container,
    run_container,
)
from .logger import success
from .report import Report, TestResult, now, validate_report, write_json, write_tarball

logger = logging.getLogger("ci")

T = TypeVar("T")

CONTAINER_TESTS = ("Services", "Ports", "Web response", "Take screenshot", "Runtime probe")
"""The tests that need the container up after init: all SKIP when it is not."""

TEST_ORDER = (
    "Container startup",
    "Services",
    "Ports",
    "Web response",
    "Runtime log check",
    "Get package info",
    "Create SBOM",
    "File tree",
    "Layers",
    "Init graph",
    "Take screenshot",
    "Runtime probe",
)


class Skip(Exception):
    """Raised by a step that does not apply to this image; recorded as SKIP with the message."""


class Run:
    """State for one harness run. Every step records a TestResult and never raises."""

    def __init__(self, settings: Settings, client: DockerClient, work: Path) -> None:
        self.settings = settings
        self.client = client
        self.work = work
        self.out = settings.out_dir
        self.arch = settings.arch
        self.manifest = settings.manifest
        self.results: dict[str, TestResult] = {}
        self.report = Report(
            image=self.manifest.name,
            tag=settings.tag,
            arch=settings.arch,
            started=now(),
            engine=engine_version(client),
            runner=platform.node() or None,
        )
        self.inspect: dict[str, Any] = {}
        self.container_log = ""
        self.app_logs = ""
        self.processes: list[checks.Process] = []
        self.syft: packages.SyftResult | None = None
        self.syft_error = ""

    # -- helpers --------------------------------------------------------------------------------------------

    def record(self, name: str, status: str, message: str = "", seconds: float | None = None) -> None:
        self.results[name] = TestResult(name, status, message, seconds)
        if status == "PASS":
            success(logger, "%s: %s %s", name, status, message)
        elif status in ("WARN", "SKIP"):
            logger.warning("%s: %s %s", name, status, message)
        else:
            logger.error("%s: %s %s", name, status, message)

    def skip_container_tests(self, reason: str) -> None:
        for name in CONTAINER_TESTS:
            self.record(name, "SKIP", reason)

    def check(self, name: str, fn: Callable[[], checks.CheckResult], warning_key: str | None = None) -> None:
        """Run a check that decides its own status; an exception is a FAIL like in `step`."""
        start = time.monotonic()
        try:
            result = fn()
        except Exception as err:
            logger.exception("%s failed", name)
            self.record(name, "FAIL", f"{type(err).__name__}: {err}", time.monotonic() - start)
            return
        self.record(name, result.status, result.message, time.monotonic() - start)
        if warning_key and result.warning:
            self.report.warnings[warning_key] = result.warning

    def step(self, name: str, fn: Callable[[], str]) -> bool:
        """Run `fn`, which returns a PASS message; any exception becomes a FAIL for `name`."""
        start = time.monotonic()
        try:
            message = fn()
        except Skip as skip:
            self.record(name, "SKIP", str(skip), time.monotonic() - start)
            return True
        except Exception as err:
            logger.exception("%s failed", name)
            self.record(name, "FAIL", f"{type(err).__name__}: {err}", time.monotonic() - start)
            return False
        self.record(name, "PASS", message, time.monotonic() - start)
        return True

    def asset(self, name: str) -> Path:
        return self.out / name.replace("<arch>", self.arch)

    # -- the run --------------------------------------------------------------------------------------------

    def execute(self) -> None:
        self.inspect = inspect_image(self.client, self.settings.image)
        self.out.mkdir(parents=True, exist_ok=True)
        write_json(self.asset("image-config.<arch>.json"), self.inspect)
        logger.info("testing %s (%s, %s mode)", self.settings.image, self.arch, self.settings.mode)

        if self.manifest.test.enabled:
            self.run_container_tests()
        else:
            self.record("Container startup", "SKIP", "test.enabled is false in the manifest")
            self.skip_container_tests("container was not started")
            self.run_syft()

        self.package_outputs()
        self.static_outputs()
        self.write_report()

    def run_container_tests(self) -> None:
        env = {"S6_VERBOSITY": "2", **self.manifest.test.env}
        try:
            container = run_container(self.client, self.settings.image, env)
        except DockerException as err:
            logger.error("could not start the container: %s", err)
            self.record("Container startup", "FAIL", f"could not start the container: {err}")
            self.skip_container_tests("container was not started")
            self.run_syft()
            return
        logger.info("started container %s", container.short_id)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ci") as pool:
                syft_future = pool.submit(self.run_syft)
                startup_future = pool.submit(startup.wait_for_init, container, float(self.settings.startup_timeout))
                result = startup_future.result()
                syft_future.result()
            self.report.startup_seconds = result.seconds
            self.record("Container startup", "PASS" if result.success else "FAIL", result.message, result.seconds)
            if result.success:
                self.observe(container)
            else:
                self.skip_container_tests("container did not finish starting")
        finally:
            if container_running(container):
                self.processes = checks.list_processes(container)
                self.app_logs = checks.read_app_logs(container)
                if self.app_logs:
                    (self.work / "app_logs.txt").write_text(self.app_logs, encoding="utf-8")
            self.container_log = container_logs(container, timestamps=True)
            (self.work / "container.log").write_text(self.container_log, encoding="utf-8")
            remove_container(container)

    def observe(self, container: Container) -> None:
        before = smoke.service_status(container)
        start = time.monotonic()
        ip = container_ip(container)

        self.check("Ports", lambda: smoke.ports_check(container, self.manifest.required_ports), "ports")
        web = self.manifest.test.web
        url = f"{web.scheme}://{ip}:{web.port}{web.path}" if web else ""
        if web is None:
            self.record("Web response", "SKIP", "manifest has no test.web")
        elif not ip:
            self.record("Web response", "FAIL", "container has no IP address")
        else:
            self.check("Web response", lambda: smoke.web_check(url, web.auth, web.screenshot_timeout))

        def do_probe() -> str:
            self.report.runtime = probe.probe(container)
            observed = self.report.runtime
            tcp = observed.get("listening_tcp", [])
            return f"{observed.get('pids', '?')} pids, tcp {tcp}, rss {observed.get('rss_bytes', 0)} bytes"

        self.step("Runtime probe", do_probe)
        (self.work / "sysinfo.txt").write_text(checks.collect_sysinfo(container), encoding="utf-8")
        web_result = self.results.get("Web response")
        if web_result is not None and web_result.status == "FAIL":
            self.record("Take screenshot", "SKIP", "the web endpoint did not answer (see Web response)")
        else:
            self.screenshot(url if ip else "")

        remaining = smoke.SERVICE_WINDOW - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(remaining)
        after = smoke.service_status(container)
        self.check("Services", lambda: smoke.services_check(before, after, time.monotonic() - start))

    def screenshot(self, url: str) -> None:
        if self.settings.web is None:
            reason = "SCREENSHOT=false" if self.manifest.test.web else "manifest has no test.web"
            self.record("Take screenshot", "SKIP", reason)
            return
        if not url:
            self.record("Take screenshot", "FAIL", "container has no IP address")
            return
        start = time.monotonic()
        try:
            shot = browser.take_screenshot(
                url,
                self.work / "screenshot.png",
                self.settings.web.auth,
                self.settings.screenshot_delay,
                self.settings.screenshot_timeout,
            )
        except Exception as err:
            logger.exception("screenshot failed")
            self.record("Take screenshot", "FAIL", f"{type(err).__name__}: {err}", time.monotonic() - start)
            return
        if shot.browser_log:
            (self.work / "browser.log").write_text(shot.browser_log, encoding="utf-8")
        self.record("Take screenshot", "PASS" if shot.success else "FAIL", shot.message, shot.seconds)

    def run_syft(self) -> None:
        try:
            self.syft = packages.run_syft(self.settings.image, self.settings.syft_timeout)
        except packages.SyftError as err:
            self.syft_error = str(err)
            logger.error("syft failed: %s", err)

    def package_outputs(self) -> None:
        table = self.syft.table if self.syft else ""
        if self.syft is None:
            self.record("Get package info", "FAIL", self.syft_error)
            self.record("Create SBOM", "FAIL", self.syft_error)
        else:
            data = self.syft.data
            self.asset("package_versions.<arch>.txt").write_text(table, encoding="utf-8")
            self.record("Get package info", "PASS", f"{packages.package_count(data)} packages")
            self.step("Create SBOM", lambda: self._write_sbom(data))

        diff = packages.package_diff(self.settings.previous_packages_url, table) if table else ""
        (self.work / "package_diff.txt").write_text(diff, encoding="utf-8")

        if not self.container_log:
            self.record("Runtime log check", "SKIP", "the container wrote no log")
            return
        result, warnings = checks.runtime_check(self.processes, table, (self.container_log, self.app_logs))
        self.record("Runtime log check", result.status, result.message)
        self.report.warnings.update(warnings)

    def _write_sbom(self, data: dict[str, Any]) -> str:
        path = self.asset("packages.<arch>.json.gz")
        packages.write_syft_json(data, path)
        spdx = self.asset("sbom.<arch>.spdx.json")
        packages.write_spdx(data, spdx)
        return (
            f"{packages.package_count(data)} packages: syft JSON {path.stat().st_size} bytes gzipped, "
            f"SPDX for the image attestation {spdx.stat().st_size} bytes"
        )

    def static_outputs(self) -> None:
        labels: dict[str, str] = dict(self.inspect.get("Config", {}).get("Labels") or {})
        base = layers.find_base_image(self.client, self.inspect, labels, self.manifest.build.base, self.arch)
        base_ref = base[0] if base else None
        logger.info("base image: %s", base_ref or "not found locally")

        def do_layers() -> str:
            history = image_history(self.client, self.settings.image)
            manifest_layers = layers.registry_manifest_layers(self.settings.image, self.arch)
            result = layers.build_layers(self.inspect, history, base[1] if base else None, manifest_layers)
            write_json(self.asset("layers.<arch>.json"), result)
            compressed = "compressed sizes from registry" if manifest_layers else "compressed sizes unavailable"
            return f"{len(result)} layers, {sum(1 for r in result if r.get('from_base'))} from base, {compressed}"

        self.step("Layers", do_layers)

        def do_files() -> str:
            return dumper.file_tree(self.client, self.settings.image, self.asset("files.<arch>.json.gz"))

        self.step("File tree", do_files)

        def do_initgraph() -> str:
            with tempfile.TemporaryDirectory(prefix="s6-") as tmp:
                root = Path(tmp)
                if not copy_from_image(self.client, self.settings.image, "/etc/s6-overlay/s6-rc.d", root / "image"):
                    raise Skip("not an s6 image: no /etc/s6-overlay/s6-rc.d")
                units = initgraph.read_units(root / "image" / "s6-rc.d")
                base_names: set[str] | None = None
                if base_ref and copy_from_image(self.client, base_ref, "/etc/s6-overlay/s6-rc.d", root / "base"):
                    base_names = {u.name for u in initgraph.read_units(root / "base" / "s6-rc.d")}
            tag = self.manifest.release.tag if self.settings.mode == "live" else self.settings.tag
            header = f"{self.manifest.name}:{tag}"
            labelled = layers.base_image_ref(labels, self.manifest.build.base)
            base_images = [initgraph.short_image_name(base_ref or labelled or f"baseimage-{self.manifest.build.base}")]
            graph = initgraph.build_init_graph(units, header, base_images, base_names)
            write_json(self.asset("init-graph.<arch>.json"), graph)
            own = sum(1 for u in graph["units"] if u["source"] == "image")
            return f"{len(units)} units, {own} from this image, base units {'compared' if base_names else 'assumed'}"

        self.step("Init graph", do_initgraph)

    def write_report(self) -> None:
        for name in TEST_ORDER:
            if name in self.results:
                self.report.add(self.results[name])
        for name, result in self.results.items():
            if name not in TEST_ORDER:
                self.report.add(result)
        self.report.finished = now()

        self.report.files = write_tarball(self.work, self.asset("test.<arch>.tar.gz"))

        record = self.report.as_dict()
        violations = validate_report(record)
        for violation in violations:
            logger.error("test report does not match test-report.schema.json: %s", violation)
        write_json(self.asset("test.<arch>.json"), record)
        if self.report.success:
            success(logger, "all tests passed for %s", self.settings.image)
        else:
            logger.error("tests FAILED for %s", self.settings.image)


def run(settings: Settings) -> int:
    """Run everything. Raises DockerUnavailable or OSError when the harness itself cannot work; returns 0 otherwise."""
    client = connect()
    work = Path(tempfile.mkdtemp(prefix="ci-report-"))
    try:
        Run(settings, client, work).execute()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0
