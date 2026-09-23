"""Offline tests of the bot boundary, persistence, and concurrent commands."""

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from scripts import bot


def fake_update(chat_id=101):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()))


def messages(update):
    return "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "runs.sqlite3"
        self.store = bot.RunStore(self.db_path)
        self.runner = Mock(side_effect=bot.mock_run_demo)
        self.handlers = bot.BotHandlers(self.store, "mock", self.runner)
        self.context = SimpleNamespace(args=[])

    async def test_run_last_history_and_restart(self):
        run = fake_update()
        await self.handlers.run(run, self.context)
        self.runner.assert_called_once_with(42)
        self.assertIn("МОК", messages(run))
        self.assertIn("+184 250.50", messages(run))
        self.assertIn("16 800.00", messages(run))
        self.assertIn("Пилоты: 6", messages(run))
        self.assertIn("tariff_8", messages(run))
        row = self.store.recent(101)[0]
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["seed"], 42)
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(json.loads(row["campaigns_json"]), bot.mock_run_demo()["campaigns"])
        restarted = bot.BotHandlers(bot.RunStore(self.db_path), "mock", self.runner)
        last, history = fake_update(), fake_update()
        await restarted.last(last, self.context)
        await restarted.history(history, self.context)
        self.assertIn("+184 250.50", messages(last))
        self.assertIn("Запуск #1", messages(history))
        self.runner.assert_called_once()

    async def test_history_shows_latest_five_and_isolates_chats(self):
        for _ in range(7):
            await self.handlers.run(fake_update(), self.context)
        await self.handlers.run(fake_update(202), self.context)
        update = fake_update()
        await self.handlers.history(update, self.context)
        text = messages(update)
        self.assertEqual(text.count("Запуск #"), 5)
        self.assertIn("Запуск #7", text)
        self.assertIn("Запуск #3", text)
        self.assertNotIn("Запуск #2", text)
        self.assertNotIn("Запуск #8", text)
        self.assertLess(text.index("#7"), text.index("#6"))
        self.assertEqual(len(self.store.recent(202)), 1)
        self.assertEqual(self.store.recent(303), [])

    async def test_empty_history_and_argument_rejection(self):
        update = fake_update()
        await self.handlers.last(update, self.context)
        await self.handlers.history(update, self.context)
        await self.handlers.run(update, SimpleNamespace(args=["99"]))
        self.assertIn("нет запусков", messages(update))
        self.assertIn("История пуста", messages(update))
        self.assertIn("без аргументов", messages(update))
        self.runner.assert_not_called()

    async def test_error_is_saved_and_visible_in_last_and_history(self):
        self.runner.side_effect = RuntimeError("eval failed")
        update = fake_update()
        await self.handlers.run(update, self.context)
        row = self.store.recent(101)[0]
        self.assertEqual(row["state"], "error")
        self.assertIn("RuntimeError: eval failed", row["error"])
        self.assertIsNone(row["metrics_json"])
        self.assertIn("Ошибка", messages(update))
        last, history = fake_update(), fake_update()
        await self.handlers.last(last, self.context)
        await self.handlers.history(history, self.context)
        self.assertIn("eval failed", messages(last))
        self.assertIn("eval failed", messages(history))
        # Error does not leave the run lock held.
        self.runner.side_effect = bot.mock_run_demo
        await self.handlers.run(fake_update(), self.context)
        self.assertEqual(self.store.recent(101)[0]["state"], "done")

    async def test_negative_score_and_no_campaigns_are_results_not_exceptions(self):
        result = bot.mock_run_demo()
        result["campaigns"] = []
        result["metrics"].update(net_arpu_gain=-100, total_cost=0, status="FAIL")
        self.runner.side_effect = None
        self.runner.return_value = result
        update = fake_update()
        await self.handlers.run(update, self.context)
        self.assertEqual(self.store.recent(101)[0]["state"], "done")
        self.assertIn("FAIL", messages(update))
        self.assertIn("-100.00", messages(update))
        self.assertIn("Нет финальных кампаний", messages(update))

    async def test_concurrent_run_is_rejected_while_history_is_responsive(self):
        entered, release = threading.Event(), threading.Event()

        def slow_runner(seed):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release runner")
            return bot.mock_run_demo(seed)

        self.runner.side_effect = slow_runner
        first = asyncio.create_task(self.handlers.run(fake_update(), self.context))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            last = fake_update()
            await asyncio.wait_for(self.handlers.last(last, self.context), 1)
            self.assertIn("Выполняется", messages(last))
            second = fake_update(202)
            await asyncio.wait_for(self.handlers.run(second, self.context), 1)
            self.assertIn("уже выполняется", messages(second))
            self.runner.assert_called_once_with(42)
            self.assertEqual(self.store.recent(202), [])
        finally:
            release.set()
            await first
        self.assertEqual(self.store.recent(101)[0]["state"], "done")

    async def test_result_persists_if_telegram_delivery_fails(self):
        update = fake_update()
        update.effective_message.reply_text.side_effect = [None, RuntimeError("offline")]
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.handlers.run(update, self.context)
        self.assertEqual(self.store.recent(101)[0]["state"], "done")
        self.assertFalse(self.handlers.run_lock.locked())

    async def test_long_unicode_response_fits_telegram_limit(self):
        text = "🚀<>&" * 3000
        update = fake_update()
        await bot.reply(update.effective_message, text)
        parts = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts))
        self.assertTrue(all(call.kwargs["parse_mode"] is None
                            for call in update.effective_message.reply_text.await_args_list))

    async def test_invalid_eval_result_is_saved_as_error(self):
        self.runner.side_effect = lambda seed: {"seed": seed}
        await self.handlers.run(fake_update(), self.context)
        self.assertEqual(self.store.recent(101)[0]["state"], "error")

    def test_interrupted_run_is_recovered_after_restart(self):
        self.store.start(101, 42, "mock")
        restarted = bot.RunStore(self.db_path)
        restarted.recover_interrupted()
        row = restarted.recent(101)[0]
        self.assertEqual(row["state"], "error")
        self.assertIn("прерван", row["error"])
        self.assertIsNotNone(row["finished_at"])

    def test_token_is_redacted_before_storage(self):
        token = "123456789:" + "Secret" * 6
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token}):
            run_id = self.store.start(101, 42, "mock")
            bot.execute_run(self.store, run_id, Mock(side_effect=RuntimeError(token)), 42)
        error = self.store.recent(101)[0]["error"]
        self.assertNotIn(token, error)
        self.assertIn("[TOKEN]", error)


class ContractTests(unittest.TestCase):
    def test_invalid_contract_values(self):
        cases = [("seed", 99), ("seed", True), ("n_pilots", -1), ("n_pilots", 1.5),
                 ("campaigns", {}), ("campaigns", [{"channel": "sms"}]),
                 ("metrics.net_arpu_gain", float("nan")),
                 ("metrics.total_cost", float("inf")), ("metrics.total_cost", -1),
                 ("metrics.total_contacts", 2.5), ("metrics.total_contacts", True),
                 ("metrics.status", ""), ("metrics.status", None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                result = copy.deepcopy(bot.mock_run_demo())
                if field.startswith("metrics."):
                    result["metrics"][field.split(".")[1]] = value
                else:
                    result[field] = value
                with self.assertRaises(ValueError):
                    bot.validate_result(result, 42)

    def test_unrelated_infinite_roi_does_not_break_agreed_metrics(self):
        result = bot.mock_run_demo()
        result["metrics"]["roi"] = float("inf")
        clean = bot.validate_result(result, 42)
        self.assertEqual(clean, bot.mock_run_demo())
        json.dumps(clean, allow_nan=False)

    def test_real_backend_calls_only_run_demo_once(self):
        with patch("scripts.local_eval.run_demo", side_effect=bot.mock_run_demo) as run_demo:
            result = bot.load_runner("real")(42)
        run_demo.assert_called_once_with(42)
        self.assertEqual(result["seed"], 42)

    def test_missing_real_backend_never_falls_back_to_mock(self):
        with patch.dict("sys.modules", {"scripts.local_eval": None}):
            with self.assertRaises(ModuleNotFoundError):
                bot.load_runner("real")(42)

    def test_mock_does_not_import_eval(self):
        with patch.dict("sys.modules", {"scripts.local_eval": None,
                                       "scripts.agent": None, "scripts.scoring_core": None}):
            self.assertEqual(bot.load_runner("mock")(42)["seed"], 42)

    def test_offline_module_cli_with_windows_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cli.sqlite3"
            environment = dict(os.environ, PYTHONIOENCODING="cp1252")
            environment.pop("TELEGRAM_BOT_TOKEN", None)
            result = subprocess.run(
                [sys.executable, "-m", "scripts.bot", "--demo", "--backend", "mock",
                 "--db", str(path)], cwd=bot.ROOT, env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("МОК", result.stdout)
            self.assertIn("184 250.50", result.stdout)
            self.assertEqual(bot.RunStore(path).recent(0)[0]["state"], "done")


if __name__ == "__main__":
    unittest.main()
