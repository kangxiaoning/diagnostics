from __future__ import annotations

import logging
import pathlib
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# Log directory relative to project root
LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "log"


def _ensure_log_dir() -> pathlib.Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def get_log_path(suffix: str = "") -> pathlib.Path:
    """Return the active log file path, optionally with a suffix.

    The filename carries NO date; per-day rotation is handled by
    TimedRotatingFileHandler (archives become <name>.log.<YYYY-MM-DD>),
    so file names always match content dates.

    Examples:
        get_log_path()           → log/diagnostics.log
        get_log_path("access")   → log/access.log
    """
    name = "diagnostics.log" if not suffix else f"{suffix}.log"
    return _ensure_log_dir() / name


def setup_logging(*, level: int = logging.DEBUG) -> None:
    """Configure project-wide logging to both file and console.

    File log:  log/diagnostics.log (DEBUG and above; rotates daily →
           archives log/diagnostics.log.<YYYY-MM-DD>)
    Console:   stderr  (INFO and above)

    Call once at startup (e.g. in main.py).
    """
    log_file = get_log_path()
    _ensure_log_dir()

    root = logging.getLogger()
    root.setLevel(level)

    # Clear any existing handlers (e.g. from uvicorn reload)
    for h in list(root.handlers):
        root.removeHandler(h)

    # File handler – rotate at midnight, keep 3 days
    file_handler = TimedRotatingFileHandler(
        str(log_file), when="midnight", interval=1,
        backupCount=3, encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(file_handler)

    # Console handler – brief logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console_handler)

    # Quiet down noisy third-party loggers
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info("Logging initialized → %s", log_file)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name (typically __name__)."""
    return logging.getLogger(name)
