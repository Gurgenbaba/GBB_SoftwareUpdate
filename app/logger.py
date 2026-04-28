from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import LOG_DIR


class UILogHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        self._callback(self.format(record))


def build_logger(ui_callback: Callable[[str], None] | None = None) -> tuple[logging.Logger, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = LOG_DIR / f"updater_{timestamp}.log"

    logger = logging.getLogger("gbb_updater")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(logfile, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if ui_callback is not None:
        ui_handler = UILogHandler(ui_callback)
        ui_handler.setFormatter(formatter)
        logger.addHandler(ui_handler)

    return logger, logfile
