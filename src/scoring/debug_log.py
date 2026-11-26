"""A log file for --debug commands: src/logs/<date>.log, one file per day.

Each model call is written with its full prompt and response, so the prompts
can be checked. Files older than DEBUG_LOG_KEEP_DAYS are deleted when a new
--debug command starts. Online (without --debug) nothing is written here.
"""

import itertools
import logging
import re
import time
from collections.abc import Callable
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from src.scoring.config import DEBUG_LOG_KEEP_DAYS
from src.scoring.runner import BlockedError, RefusedError

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
FILE = re.compile(r"(\d{4}-\d{2}-\d{2})\.log")

logger = logging.getLogger("scoring")
# Without a log file, messages go nowhere (not to the screen).
logger.addHandler(logging.NullHandler())
logger.propagate = False


def start(directory: Path | None = None, keep_days: int = DEBUG_LOG_KEEP_DAYS, today: date | None = None) -> Path:
    """Delete old files, then send log messages to today's file. Return its path."""
    directory = Path(directory or LOG_DIR)
    today = today or date.today()
    directory.mkdir(parents=True, exist_ok=True)
    remove_old(directory, keep_days, today)
    path = directory / f"{today.isoformat()}.log"
    for handler in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return path


def remove_old(directory: Path, keep_days: int, today: date) -> list[Path]:
    """Delete dated log files from `keep_days` days ago or earlier. Return what was deleted."""
    removed = []
    for path in directory.iterdir():
        match = FILE.fullmatch(path.name)
        if match and date.fromisoformat(match.group(1)) <= today - timedelta(days=keep_days):
            path.unlink()
            removed.append(path)
    return removed


def logged(complete: Callable) -> Callable:
    """Wrap a model call so each prompt, response and error is written to the log."""
    numbers = itertools.count(1)

    @wraps(complete)
    def call(model: str, prompt: str, max_tokens: int, temperature: float, schema: dict | None) -> str:
        number, kind = next(numbers), "judge" if schema else "answer"
        asked = "provider defaults" if temperature is None else f"temperature={temperature}"
        header = f"call {number} {kind} model={model} max_tokens={max_tokens} {asked}"
        started = time.monotonic()
        try:
            result = complete(model, prompt, max_tokens, temperature, schema)
        except (BlockedError, RefusedError) as exc:
            logger.warning("%s %s after %.2fs: %s\n--- prompt ---\n%s\n", header, type(exc).__name__,
                           time.monotonic() - started, exc, prompt)
            raise
        except Exception:
            logger.exception("%s failed after %.2fs\n--- prompt ---\n%s\n", header, time.monotonic() - started, prompt)
            raise
        text = result if isinstance(result, str) else result.text
        used = "" if isinstance(result, str) else f"\n--- settings used ---\n{getattr(result, 'settings', {})}"
        logger.info("%s %.2fs\n--- prompt ---\n%s\n--- response ---\n%s%s\n", header, time.monotonic() - started,
                    prompt, text, used)
        return result

    return call
