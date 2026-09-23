"""One application logger with idempotent setup, without changing the root logger."""

import logging

from config import LoggingConfig


logger = logging.getLogger("tariff_agent")
_handler: logging.StreamHandler | None = None


def configure_logging(settings: LoggingConfig) -> None:
    global _handler
    formatter = logging.Formatter(settings.format)
    if _handler is None:
        _handler = logging.StreamHandler()
        logger.addHandler(_handler)
    _handler.setFormatter(formatter)
    logger.setLevel(settings.level)
    logger.propagate = False
