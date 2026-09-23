"""Application settings, private config.json loading, and safe logging."""

from copy import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class HistoryConfig:
    relative_path: str = "data/change_tariff.csv"
    min_arpu: float = 100
    min_samples: int = 10
    arpu_thresholds: tuple[float, float] = (1000, 5000)
    arpu_labels: tuple[str, str, str] = ("LOW", "MID", "HIGH")
    lift_bounds: tuple[float, float] = (-1, 3)

    def __post_init__(self):
        if self.min_arpu <= 0 or self.min_samples < 1:
            raise ValueError("History min_arpu and min_samples must be positive")
        if len(self.arpu_thresholds) != 2 or not self.arpu_thresholds[0] < self.arpu_thresholds[1]:
            raise ValueError("History arpu_thresholds must contain two increasing values")
        if len(self.arpu_labels) != 3 or len(set(self.arpu_labels)) != 3:
            raise ValueError("History arpu_labels must contain three distinct labels")
        if len(self.lift_bounds) != 2 or self.lift_bounds[0] > self.lift_bounds[1]:
            raise ValueError("History lift_bounds must contain an increasing pair")


@dataclass(frozen=True)
class StrategyConfig:
    min_segment_size: int = 300
    max_segment_size: int = 5000
    max_pilots: int = 12
    pilot_size: int = 150
    max_campaigns: int = 10
    prior_weight: float = 50
    per_customer_std: float = 0.8
    uncertainty_penalty: float = 0.5

    def __post_init__(self):
        for name, lower, upper in (
            ("min_segment_size", 1, 5000), ("max_segment_size", 1, 5000),
            ("max_pilots", 1, 20), ("pilot_size", 10, 200), ("max_campaigns", 1, 10),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"Strategy {name} must be an integer in [{lower}, {upper}]")
        if self.min_segment_size > self.max_segment_size:
            raise ValueError("Strategy min_segment_size exceeds max_segment_size")
        for name in ("prior_weight", "per_customer_std", "uncertainty_penalty"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"Strategy {name} must be finite and non-negative")


@dataclass(frozen=True)
class GeminiConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    model: str = "gemini-3.6-flash"
    timeout_ms: int = 10_000
    max_attempts: int = 1
    temperature: float = 0
    max_output_tokens: int = 1024
    thinking_budget: int = 0
    ranking_prompt: str = """Rank exactly {max_pilots} distinct candidate IDs for SMS pilots.
Return a JSON array of IDs in priority order. Maximize expected net ARPU gain
while balancing audience value and historical support. Historical transition
shares are only proxies, and effects may differ on this audience.
Each pilot samples {pilot_size} customers at cost {sms_cost} each.
Final decisions will use pilot results.
Candidates: {candidates_json}
"""

    def __post_init__(self):
        if type(self.enabled) is not bool or not isinstance(self.api_key, str):
            raise ValueError("Gemini enabled must be boolean and api_key must be a string")
        if self.timeout_ms <= 0 or self.max_attempts < 1 or self.max_output_tokens < 1:
            raise ValueError("Gemini timeout, attempts and output token limit must be positive")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "%(levelname)s %(name)s: %(message)s"

    def __post_init__(self):
        if self.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Logging level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")


@dataclass(frozen=True)
class BotConfig:
    backend: str = "real"
    db_path: str = "data/bot/runs.sqlite3"

    def __post_init__(self):
        if self.backend not in ("mock", "real"):
            raise ValueError("bot.backend must be mock or real")
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ValueError("bot.db_path must be a non-empty path")


@dataclass(frozen=True)
class AgentConfig:
    history: HistoryConfig = field(default_factory=HistoryConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    bot: BotConfig = field(default_factory=BotConfig)


def load_config(path: str | Path | None = None) -> AgentConfig:
    """Missing files/fields use defaults; malformed or unknown settings fail clearly."""
    path = ROOT / (path if path is not None else "config.json")
    if not path.exists():
        return AgentConfig()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"history", "strategy", "gemini", "logging", "bot"}:
        raise ValueError("Config must contain only history, strategy, gemini, logging and bot sections")
    if any(not isinstance(section, dict) for section in data.values()):
        raise ValueError("Each config section must be an object")
    history = dict(data.get("history", {}))
    bot = dict(data.get("bot", {}))
    # Accept the old default without keeping a configurable import mechanism.
    if bot.get("runner_module") == "bot_eval":
        del bot["runner_module"]
    if "runner_module" in bot:
        raise ValueError("bot.runner_module is no longer supported; remove it to use local_eval")
    for key in ("arpu_thresholds", "arpu_labels", "lift_bounds"):
        if key in history:
            history[key] = tuple(history[key])
    return AgentConfig(
        history=HistoryConfig(**history),
        strategy=StrategyConfig(**data.get("strategy", {})),
        gemini=GeminiConfig(**data.get("gemini", {})),
        logging=LoggingConfig(**data.get("logging", {})),
        bot=BotConfig(**bot),
    )


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
