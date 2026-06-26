from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from mn_api.config import LoggingConfig
from mn_api.path_utils import default_logs_root


def configure_logging(name: str = "mn-api", default_file: str = "api.log") -> logging.Logger:
    config = LoggingConfig.from_env()
    logger = logging.getLogger(name)
    logger.setLevel(config.level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    log_path = config.api_log_path if default_file == "api.log" else default_logs_root() / default_file

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
        )
    except OSError:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
