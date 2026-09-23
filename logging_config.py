"""One application logger with idempotent setup, without changing the root logger."""

from copy import copy
import logging
import os
import re

from config import LoggingConfig


logger = logging.getLogger("tariff_agent")
_handler: logging.StreamHandler | None = None


def redact_token(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        text = text.replace(token, "[TOKEN]")
    return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[TOKEN]", text)


class SafeFormatter(logging.Formatter):
    def formatException(self, exc_info) -> str:
        # SDK exceptions can embed credentials in their message and traceback.
        return exc_info[0].__name__

    def format(self, record: logging.LogRecord) -> str:
        record = copy(record)
        record.exc_text = None  # Discard a traceback cached by another formatter.
        return redact_token(super().format(record))


def configure_logging(settings: LoggingConfig) -> None:
    global _handler
    formatter = SafeFormatter(settings.format)
    if _handler is None:
        _handler = logging.StreamHandler()
        logger.addHandler(_handler)
    _handler.setFormatter(formatter)
    logger.setLevel(settings.level)
    logger.propagate = False
    # PTB logs bootstrap failures before main() can catch them. Route SDK logs
    # through the safe formatter instead of the root/last-resort handler.
    for name in ("telegram", "httpx", "httpcore"):
        external = logging.getLogger(name)
        external.addHandler(_handler)
        external.setLevel(logging.WARNING)
        external.propagate = False
