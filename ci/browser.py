"""Screenshot of the web UI with headless Chrome through Selenium, plus the browser console log."""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import urllib3
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger("browser")

CHROME_BINARIES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


@dataclass
class ScreenshotResult:
    success: bool
    message: str
    seconds: float
    browser_log: str = ""
    screenshot: Path | None = None


def chrome_binary() -> str | None:
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def make_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    for arg in (
        "--no-sandbox",
        "--test-type",
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--window-size=1920,1080",
        "--ignore-certificate-errors",
    ):
        options.add_argument(arg)
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    options.set_capability("acceptInsecureCerts", True)
    binary = chrome_binary()
    if binary:
        options.binary_location = binary
    driver_path = shutil.which("chromedriver")
    service = Service(executable_path=driver_path) if driver_path else Service()
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(60)
    return driver


def endpoint_ok(url: str, auth: tuple[str, str] | None) -> bool:
    try:
        response = requests.get(url, timeout=10, verify=False, auth=auth)
        response.raise_for_status()
        return True
    except requests.RequestException as err:
        logger.debug("endpoint %s not ready: %s", url, err)
        return False


def browser_log(driver: webdriver.Chrome) -> str:
    try:
        entries: list[dict[str, Any]] = driver.get_log("browser")  # type: ignore[no-untyped-call]
        return json.dumps(entries, indent=2)
    except WebDriverException as err:
        return json.dumps({"error": f"could not read browser log: {err}"})


def split_auth(auth: str | None) -> tuple[str, str] | None:
    if not auth or ":" not in auth:
        return None
    user, password = auth.split(":", 1)
    return user, password


def take_screenshot(
    url: str,
    out_path: Path,
    auth: str | None,
    delay: int,
    timeout: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    driver_factory: Callable[[], webdriver.Chrome] = make_driver,
) -> ScreenshotResult:
    """Retry until the endpoint answers and a screenshot is saved, or `timeout` seconds pass."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    start = clock()
    deadline = start + timeout
    credentials = split_auth(auth)
    browser_url = url
    if credentials:
        scheme, rest = url.split("://", 1)
        browser_url = f"{scheme}://{auth}@{rest}"
    driver: webdriver.Chrome | None = None
    last_error = "endpoint never answered"
    try:
        driver = driver_factory()
        while clock() < deadline:
            if not endpoint_ok(url, credentials):
                sleep(3)
                continue
            try:
                driver.get(browser_url)
                sleep(delay)
                driver.save_screenshot(str(out_path))
                if not out_path.is_file():
                    raise FileNotFoundError(str(out_path))
                seconds = clock() - start
                return ScreenshotResult(True, f"screenshot of {url}", seconds, browser_log(driver), out_path)
            except (WebDriverException, OSError) as err:
                last_error = f"{type(err).__name__}: {str(err).splitlines()[0] if str(err) else ''}"
                logger.debug("screenshot attempt failed: %s", last_error)
                sleep(3)
        message = f"timed out after {timeout}s: {last_error}"
        return ScreenshotResult(False, message, clock() - start, browser_log(driver))
    except WebDriverException as err:
        message = f"browser failed to start: {str(err).splitlines()[0] if str(err) else err!r}"
        logger.error(message)
        return ScreenshotResult(False, message, clock() - start)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                logger.debug("driver.quit failed", exc_info=True)
