"""python -m ci: run the harness. Exit 0 whenever the harness ran, even if tests failed."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_settings
from .docker_client import DockerUnavailable
from .harness import run
from .logger import configure_logging


def main(argv: list[str] | None = None) -> int:
    configure_logging(os.environ.get("CI_LOG_LEVEL", "INFO") or "INFO")
    logger = logging.getLogger("ci")
    out_dir = Path(os.environ.get("CI_OUT_DIR", "/out"))
    try:
        settings = load_settings(os.environ, out_dir=out_dir)
    except ConfigError as err:
        logger.error("configuration error: %s", err)
        return 2
    try:
        return run(settings)
    except DockerUnavailable as err:
        logger.error("%s", err)
        return 1
    except OSError as err:
        logger.error("cannot write results: %s", err)
        return 1
    except Exception:
        logger.exception("the harness itself failed")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
