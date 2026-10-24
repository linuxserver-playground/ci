"""Coloured stdout logging with an extra SUCCESS level."""

from __future__ import annotations

import logging
import platform
import sys

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

_COLOURS = {
    logging.DEBUG: "38",
    logging.INFO: "36",
    logging.WARNING: "33",
    logging.ERROR: "31",
    logging.CRITICAL: "31",
    SUCCESS: "32",
}

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s"


class ColourFormatter(logging.Formatter):
    """Wraps each line in the ANSI colour for its level and keeps tracebacks on one line."""

    def __init__(self, colour: bool) -> None:
        super().__init__(_FORMAT, "%Y-%m-%d %H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if record.exc_text:
            text = text.replace("\n", " | ")
        if not self.colour:
            return text
        code = _COLOURS.get(record.levelno, "38")
        return f"\x1b[{code};20m{text}\x1b[0m"


def success(logger: logging.Logger, message: str, *args: object) -> None:
    """Log at the SUCCESS level (between INFO and WARNING)."""
    if logger.isEnabledFor(SUCCESS):
        logger.log(SUCCESS, message, *args, stacklevel=2)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root handlers with one stdout handler."""
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColourFormatter(colour=sys.stdout.isatty() or True))
    root.addHandler(handler)
    for noisy in ("urllib3", "selenium", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Python %s on %s", platform.python_version(), platform.platform())
