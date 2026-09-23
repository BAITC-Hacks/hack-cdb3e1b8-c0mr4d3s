"""Telegram entry point for the real agent, with an explicit fixture mode."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Callable

from .config import ROOT, BotConfig, load_config, configure_logging, logger as LOG, redact_token

SEED = 42
FILTERS = {
    "filter_arpu_segment": "ARPU",
    "filter_data_segment": "интернет",
    "filter_call_segment": "звонки",
    "filter_current_tariff": "текущий тариф",
}


def mock_run_demo(seed: int = 42) -> dict:
    """Fixed illustrative response; no agent, environment, or scoring imports."""
    return {
        "seed": seed,
        "campaigns": [
            {"campaign_name": "Активные пользователи интернета",
             "filter_arpu_segment": "MID", "filter_data_segment": "HEAVY",
             "filter_current_tariff": "tariff_4", "target_tariff": "tariff_8",
             "channel": "sms"},
            {"campaign_name": "Премиальный сегмент",
             "filter_arpu_segment": "HIGH", "filter_current_tariff": "tariff_9",
             "target_tariff": "tariff_10", "channel": "push"},
        ],
        "n_pilots": 6,
        "metrics": {"net_arpu_gain": 184250.50, "total_cost": 16800.0,
                    "total_contacts": 6200, "status": "PASS"},
    }


def load_runner(backend: str) -> Callable[[int], dict]:
    if backend == "mock":
        return mock_run_demo
    if backend != "real":
        raise ValueError("bot.backend должен быть mock или real")

    def run_demo(seed: int = 42) -> dict:
        # Import only on /run. Missing/broken eval is recorded as an error;
        # never silently replace a failed real run with mock results.
        from .local_eval import run_demo as evaluate
        return evaluate(seed)

    return run_demo


def validate_result(result: dict, seed: int) -> dict:
    """Check the integration contract, without calculating or judging scores."""
    if not isinstance(result, dict):
        raise ValueError("run_demo должен вернуть dict")
    if (isinstance(result.get("seed"), bool)
            or not isinstance(result.get("seed"), Integral)
            or result["seed"] != seed):
        raise ValueError("run_demo вернул неверный seed")
    pilots = result.get("n_pilots")
    if isinstance(pilots, bool) or not isinstance(pilots, Integral) or pilots < 0:
        raise ValueError("n_pilots должен быть целым неотрицательным числом")
    campaigns = result.get("campaigns")
    if not isinstance(campaigns, list):
        raise ValueError("campaigns должен быть списком")
    for campaign in campaigns:
        if not isinstance(campaign, dict) or any(
            not isinstance(campaign.get(key), str) or not campaign[key].strip()
            for key in ("target_tariff", "channel")
        ):
            raise ValueError("В каждой кампании нужны target_tariff и channel")
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("metrics должен быть словарём")
    normalized = {}
    for key in ("net_arpu_gain", "total_cost", "total_contacts"):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f"metrics.{key} должен быть конечным числом")
        if key != "net_arpu_gain" and value < 0:
            raise ValueError(f"metrics.{key} не может быть отрицательным")
        if key == "total_contacts" and value != int(value):
            raise ValueError("total_contacts должен быть целым числом")
        normalized[key] = int(value) if key == "total_contacts" else float(value)
    if not isinstance(metrics.get("status"), str) or not metrics["status"].strip():
        raise ValueError("metrics.status должен быть непустой строкой")
    normalized["status"] = metrics["status"]
    # Only persist agreed metrics. Eval may also return e.g. an infinite ROI.
    clean = {"seed": int(seed), "campaigns": campaigns,
             "n_pilots": int(pilots), "metrics": normalized}
    if result.get("error") is not None:
        if not isinstance(result["error"], str):
            raise ValueError("error должен быть строкой")
        clean["error"] = safe_error(result["error"])
    return json.loads(json.dumps(clean, ensure_ascii=False, allow_nan=False))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_error(error: Exception | str) -> str:
    text = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else error
    return redact_token(text)[:2000]


def load_telegram_token() -> str:
    # The team explicitly agreed to use an environment variable for this token.
    # All other application configuration remains in config.json.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and (ROOT / ".env").exists():
        from dotenv import dotenv_values
        token = (dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False)
                 .get("TELEGRAM_BOT_TOKEN") or "").strip()
        if token:
            os.environ["TELEGRAM_BOT_TOKEN"] = token
    return token


def migrate_legacy_database(path: Path) -> None:
    legacy_path = ROOT / "bot_data" / "runs.sqlite3"
    if path != (ROOT / BotConfig().db_path).resolve() or path.exists() or not legacy_path.is_file():
        return
    # SQLite backup includes committed WAL records; copying just the file can lose them.
    with tempfile.TemporaryDirectory(prefix=".migration-", dir=path.parent) as folder:
        snapshot = Path(folder) / "runs.sqlite3"
        with (closing(sqlite3.connect(legacy_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as source,
              closing(sqlite3.connect(snapshot, timeout=10)) as destination):
            source.backup(destination)
        try:
            # Publish a complete snapshot without replacing an existing destination.
            path.hardlink_to(snapshot)
        except FileExistsError:
            return
    LOG.info("История SQLite скопирована из bot_data в data/bot; исходная база сохранена.")


class RunStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrate_legacy_database(self.path)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    seed INTEGER NOT NULL,
                    backend TEXT NOT NULL,
                    state TEXT NOT NULL,
                    metrics_json TEXT,
                    campaigns_json TEXT,
                    n_pilots INTEGER,
                    error TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS bot_runs_chat ON bot_runs(chat_id, id DESC)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def recover_interrupted(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE bot_runs SET state='error', finished_at=?, error=?
                          WHERE state='running'""",
                       (utc_now(), "Запуск прерван остановкой процесса бота."))

    def start(self, chat_id: int, seed: int, backend: str) -> int:
        with closing(self.connect()) as db, db:
            cursor = db.execute("""INSERT INTO bot_runs
                (chat_id, created_at, seed, backend, state) VALUES (?, ?, ?, ?, 'running')""",
                                (chat_id, utc_now(), seed, backend))
            return cursor.lastrowid

    def finish(self, run_id: int, result: dict | None, error: str | None) -> None:
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE bot_runs SET finished_at=?, state=?, metrics_json=?,
                campaigns_json=?, n_pilots=?, error=? WHERE id=?""",
                       (utc_now(), "error" if error else "done",
                        json.dumps(result["metrics"], ensure_ascii=False) if result else None,
                        json.dumps(result["campaigns"], ensure_ascii=False) if result else None,
                        result["n_pilots"] if result else None, error, run_id))

    def recent(self, chat_id: int, limit: int = 5) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM bot_runs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                              (chat_id, limit)).fetchall()
        return [dict(row) for row in rows]


def execute_run(store: RunStore, run_id: int, runner: Callable[[int], dict], seed: int) -> None:
    try:
        result = validate_result(runner(seed), seed)
    except Exception as exc:
        store.finish(run_id, None, safe_error(exc))
        LOG.warning("Запуск #%s завершился ошибкой (%s)", run_id, type(exc).__name__)
    else:
        store.finish(run_id, result, result.get("error") or None)


def compact(value: object, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def number(value: float, signed: bool = False) -> str:
    return format(value, "+,.2f" if signed else ",.2f").replace(",", " ")


def mode_label(backend: str) -> str:
    return ("МОК — демонстрационные данные" if backend == "mock"
            else "Агент · локальная мок-среда")


def format_run(row: dict, brief: bool = False) -> str:
    lines = [f"Запуск #{row['id']} · {row['created_at']}",
             f"{mode_label(row['backend'])} · seed={row['seed']}"]
    if row["state"] == "running":
        return "\n".join(lines + ["Выполняется…"])
    if row["error"]:
        lines.append("Ошибка: " + compact(row["error"], 250 if brief else 1200))
        if row["metrics_json"] is None:
            return "\n".join(lines)
        lines.append("Результат уже проведённых пилотов:")
    metrics = json.loads(row["metrics_json"])
    campaigns = json.loads(row["campaigns_json"])
    lines += [f"Статус оценки: {compact(metrics['status'])}",
              f"Чистый результат: {number(metrics['net_arpu_gain'], signed=True)} у.е.",
              f"Расходы: {number(metrics['total_cost'])} у.е.",
              f"Контакты: {metrics['total_contacts']:,}".replace(",", " "),
              f"Пилоты: {row['n_pilots']} · Кампании: {len(campaigns)}"]
    if not brief:
        lines += ["Расходы и контакты включают пилоты.", "", "Финальные кампании:"]
        for index, campaign in enumerate(campaigns, 1):
            lines.append(f"{index}. {compact(campaign.get('campaign_name') or 'Кампания')} → "
                         f"{compact(campaign['target_tariff'])} · {compact(campaign['channel'])}")
            filters = [f"{label}: {compact(campaign[key])}" for key, label in FILTERS.items()
                       if campaign.get(key) not in (None, "")]
            lines.append("   " + ("; ".join(filters) if filters else "Вся аудитория"))
        if not campaigns:
            lines.append("Нет финальных кампаний.")
    return "\n".join(lines)


async def reply(message, text: str) -> None:
    # 1800 Unicode code points fit the 4096 UTF-16-unit Telegram limit,
    # including strings composed entirely of emoji. Plain text needs no escaping.
    for offset in range(0, len(text), 1800):
        await message.reply_text(text[offset:offset + 1800], parse_mode=None)


class BotHandlers:
    def __init__(self, store: RunStore, backend: str, runner: Callable[[int], dict]):
        self.store = store
        self.backend = backend
        self.runner = runner
        self.run_lock = asyncio.Lock()

    async def start(self, update, context) -> None:
        if update.effective_message is not None:
            from telegram import ReplyKeyboardMarkup

            text = ("HackAlem AI · Тарифные кампании\n"
                    + mode_label(self.backend) + "\n\n"
                    "Я запускаю агента, который выбирает аудиторию, тарифы и каналы кампаний, "
                    "проверяет гипотезы пилотами и показывает рассчитанный результат.\n\n"
                    "Нажмите /run и дождитесь отчёта.\n"
                    "/last — показать последний результат\n"
                    "/history — сравнить последние пять запусков\n\n"
                    "Используются тестовые данные кейса. Реальные рассылки абонентам не отправляются.")
            if self.backend == "mock":
                text += "\n\nМок показывает работу бота; цифры не являются оценкой агента."
            await update.effective_message.reply_text(
                text, parse_mode=None,
                reply_markup=ReplyKeyboardMarkup([["/run"], ["/last", "/history"]],
                                                 resize_keyboard=True),
            )

    async def run(self, update, context) -> None:
        message, chat = update.effective_message, update.effective_chat
        if message is None or chat is None:
            return
        if context.args:
            await reply(message, "Используйте /run без аргументов. Согласованный seed: 42.")
            return
        if self.run_lock.locked():
            await reply(message, "Один запуск уже выполняется. Дождитесь результата; /last и /history доступны.")
            return
        async with self.run_lock:
            await reply(message, f"Запускаю · seed={SEED}\n{mode_label(self.backend)}")
            run_id = self.store.start(chat.id, SEED, self.backend)
            await asyncio.to_thread(execute_run, self.store, run_id, self.runner, SEED)
            row = self.store.recent(chat.id, 1)[0]
            await reply(message, format_run(row))

    async def last(self, update, context) -> None:
        if update.effective_message is None or update.effective_chat is None:
            return
        rows = self.store.recent(update.effective_chat.id, 1)
        await reply(update.effective_message, format_run(rows[0]) if rows else
                    "В этом чате ещё нет запусков. Начните с /run.")

    async def history(self, update, context) -> None:
        if update.effective_message is None or update.effective_chat is None:
            return
        rows = self.store.recent(update.effective_chat.id, 5)
        text = "\n\n".join(format_run(row, brief=True) for row in rows)
        await reply(update.effective_message, text or "История пуста. Начните с /run.")


async def on_error(update, context) -> None:
    # Telegram request exceptions can include URLs containing the token.
    LOG.error("Ошибка обработки команды (%s)", type(context.error).__name__)
    if update is not None and getattr(update, "effective_message", None) is not None:
        try:
            await reply(update.effective_message,
                        "Не удалось обработать команду. Проверьте /last: результат мог сохраниться. "
                        "Если ошибка повторяется, проверьте подключение и доступ к SQLite.")
        except Exception:
            LOG.error("Не удалось отправить уведомление об ошибке")


async def on_startup(application) -> None:
    from telegram import BotCommand
    from telegram.error import TelegramError

    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Что умеет бот"),
            BotCommand("run", "Запустить агента и получить отчёт"),
            BotCommand("last", "Последний результат"),
            BotCommand("history", "Последние пять запусков"),
        ])
    except TelegramError as exc:
        LOG.warning("Не удалось обновить меню команд (%s)", type(exc).__name__)
    LOG.info("Бот подключён: https://t.me/%s — откройте его и нажмите /start", application.bot.username)


async def check_telegram_connection(token: str) -> str:
    from telegram import Bot

    # Initialization calls getMe once; context exit closes the HTTP client.
    async with Bot(token) as client:
        return client.username


def build_application(token: str, store: RunStore, backend: str):
    from telegram.ext import Application, CommandHandler

    handlers = BotHandlers(store, backend, load_runner(backend))
    application = (Application.builder().token(token).concurrent_updates(8)
                   .post_init(on_startup).build())
    for name, callback in (("start", handlers.start), ("help", handlers.start),
                           ("run", handlers.run), ("last", handlers.last),
                           ("history", handlers.history)):
        application.add_handler(CommandHandler(name, callback))
    application.add_error_handler(on_error)
    return application


def main() -> int:
    # Windows may use cp1252 for redirected stdout even with Russian messages.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="HackAlem Telegram bot")
    parser.add_argument("--backend", choices=("mock", "real"))
    parser.add_argument("--db", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--demo", action="store_true",
                         help="Run the fixed mock, save to SQLite and print; no token required")
    actions.add_argument("--once", action="store_true",
                         help="Run the configured backend once without Telegram")
    actions.add_argument("--check-config", action="store_true",
                         help="Check settings and token presence without network or secrets")
    actions.add_argument("--check-telegram", action="store_true",
                         help="Verify the token with Telegram without starting polling")
    args = parser.parse_args()
    try:
        settings = load_config()
        configure_logging(settings.logging)
    except (ValueError, TypeError, OSError) as exc:
        parser.error(f"Ошибка config.json ({type(exc).__name__}). Проверьте структуру и типы настроек.")
    backend = args.backend or ("mock" if args.demo else settings.bot.backend)
    db_path = ROOT / (args.db or Path(settings.bot.db_path))
    if args.demo and backend != "mock":
        parser.error("--demo работает только с --backend mock")
    if args.demo or args.once:
        store = RunStore(db_path)
        run_id = store.start(0, SEED, backend)
        execute_run(store, run_id, load_runner(backend), SEED)
        row = store.recent(0, 1)[0]
        print(format_run(row))
        print(f"\nSQLite: {store.path}")
        return 1 if row["error"] else 0
    try:
        token = load_telegram_token()
    except ImportError:
        parser.error("Установите зависимости: python -m pip install -r requirements.txt")
    if args.check_config:
        print(f"Источник: {mode_label(backend)}")
        print("Функция: " + ("bot.mock_run_demo" if backend == "mock" else "local_eval.run_demo"))
        print(f"SQLite: {db_path.resolve()}")
        print("Токен Telegram: " + ("задан" if token else "не задан"))
        print("Gemini: " + ("включён" if settings.gemini.enabled else "выключен"))
        return 0 if token else 1
    if not token:
        parser.error("Задайте TELEGRAM_BOT_TOKEN в окружении или .env. Без Telegram: python -m scripts.bot --once")
    try:
        from telegram.error import InvalidToken, NetworkError

        LOG.info("Проверяю подключение к Telegram…")
        username = asyncio.run(check_telegram_connection(token))
        LOG.info("Telegram принял токен: https://t.me/%s", username)
        if args.check_telegram:
            return 0
        store = RunStore(db_path)
        application = build_application(token, store, backend)
        store.recover_interrupted()
        LOG.info("Режим: %s; SQLite: %s", backend, store.path)
        application.run_polling(allowed_updates=["message"], drop_pending_updates=True)
    except ImportError:
        LOG.error("Установите зависимости: python -m pip install -r requirements.txt")
        return 1
    except InvalidToken:
        LOG.error("Telegram отклонил токен. Получите новый токен у @BotFather для вашего бота, "
                  "замените TELEGRAM_BOT_TOKEN в .env и снова запустите start_bot.cmd. "
                  "Если токен задан в окружении терминала, обновите и его: он имеет приоритет над .env.")
        return 1
    except NetworkError as exc:
        LOG.error("Не удалось соединиться с Telegram (%s). Проверьте доступ к интернету "
                  "и повторите запуск. Действительность токена не подтверждена.", type(exc).__name__)
        return 1
    except Exception as exc:
        LOG.error("Не удалось запустить polling (%s). Проверьте токен, сеть и единственный процесс бота.",
                  type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
