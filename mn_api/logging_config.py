from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(name: str = "mn-api", default_file: str = "api.log") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(os.getenv("MN_LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    log_path = Path(
        os.getenv(
            "MN_API_LOG_PATH",
            str(Path.home() / ".mn" / "logs" / default_file),
        )
    ).expanduser()

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_path,
            maxBytes=int(os.getenv("MN_LOG_MAX_BYTES", "1048576")),
            backupCount=int(os.getenv("MN_LOG_BACKUP_COUNT", "5")),
        )
    except OSError:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
