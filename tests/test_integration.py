"""Real local agent/scoring integration; no Telegram or Gemini requests."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from scripts.agent import Agent
from scripts import bot, local_eval
from scripts.config import AgentConfig, BotConfig, load_config
from tests import bot_eval


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_through_commands_matches_official_eval(self):
        with tempfile.TemporaryDirectory() as folder:
            store = bot.RunStore(Path(folder) / "runs.sqlite3")
            agent = Mock(wraps=Agent(settings=AgentConfig()))
            handlers = bot.BotHandlers(store, "real", bot.load_runner("real"))
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
                                     effective_message=SimpleNamespace(reply_text=AsyncMock()))
            context = SimpleNamespace(args=[])
            # Change working directory: the bot integration must resolve module paths.
            previous = Path.cwd()
            try:
                os.chdir(folder)
                with patch.object(local_eval, "Agent", return_value=agent):
                    await handlers.run(update, context)
                await handlers.last(update, context)
                await handlers.history(update, context)
                with patch.object(bot_eval, "Agent", return_value=Agent(settings=AgentConfig())):
                    expected = bot_eval.run_demo(42)
            finally:
                os.chdir(previous)
            agent.act.assert_called_once()
            row = store.recent(123)[0]
            self.assertEqual(row["state"], "done", row["error"])
            self.assertGreater(row["n_pilots"], 0)
            campaigns = json.loads(row["campaigns_json"])
            self.assertTrue(1 <= len(campaigns) <= 10)
            actual = json.loads(row["metrics_json"])
            # Both evaluators must find the data from outside the project directory.
            for key, value in actual.items():
                self.assertEqual(value, expected["metrics"][key], key)
            self.assertEqual(campaigns, expected["campaigns"])
            self.assertEqual(row["n_pilots"], expected["n_pilots"])
            self.assertLessEqual(actual["total_cost"], 100000)
            self.assertLessEqual(actual["total_contacts"], 15000)
            text = "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
            self.assertIn("локальная мок-среда", text)
            self.assertIn("Запуск #1", text)

    async def test_agent_crash_preserves_and_scores_pilot_contacts(self):
        def crash_after_pilot(env):
            env.run_pilot(target_tariff="tariff_8", channel="sms", n_customers=50)
            raise RuntimeError("private external details")

        with tempfile.TemporaryDirectory() as folder:
            store = bot.RunStore(Path(folder) / "runs.sqlite3")
            run_id = store.start(123, 42, "real")
            failing_agent = Mock()
            failing_agent.act.side_effect = crash_after_pilot
            with patch.object(local_eval, "Agent", return_value=failing_agent):
                await asyncio.to_thread(bot.execute_run, store, run_id, local_eval.run_demo, 42)
            row = store.recent(123)[0]
            self.assertEqual(row["state"], "error")
            self.assertEqual(row["n_pilots"], 1)
            metrics = json.loads(row["metrics_json"])
            self.assertEqual(metrics["total_contacts"], 50)
            self.assertEqual(metrics["total_cost"], 200)
            self.assertNotIn("private external details", row["error"])
            self.assertIn("RuntimeError", row["error"])
            self.assertIn("проведённых пилотов", bot.format_run(row))
            self.assertIn("Контакты: 50", bot.format_run(row))

    def test_empty_agent_result_is_scored_without_diverging_from_core(self):
        with patch.object(local_eval, "Agent") as agent_class:
            agent_class.return_value.act.return_value = []
            result = local_eval.run_demo(42)
        self.assertEqual(result["n_pilots"], 0)
        self.assertEqual(result["campaigns"], [])
        self.assertEqual(result["metrics"]["total_cost"], 0)
        self.assertEqual(result["metrics"]["status"], "FAIL")


class ConfigTests(unittest.TestCase):
    def test_bot_settings_do_not_change_agent_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"bot": {"backend": "mock", "runner_module": "bot_eval"}}))
            settings = load_config(path)
        self.assertEqual(settings.bot, BotConfig(backend="mock"))
        self.assertEqual(settings.strategy, AgentConfig().strategy)
        self.assertFalse(settings.gemini.enabled)

    def test_removed_runner_setting_fails_instead_of_silently_switching_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"bot": {"runner_module": "demo_eval"}}))
            with self.assertRaisesRegex(ValueError, "runner_module"):
                load_config(path)

    def test_malformed_bot_settings_fail(self):
        for kwargs in ({"backend": "production"},
                       {"db_path": ""}, {"db_path": 42}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BotConfig(**kwargs)

    def test_dotenv_loads_only_token_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text('TELEGRAM_BOT_TOKEN="file-token"\nBOT_BACKEND=mock\n')
            with patch.object(bot, "ROOT", root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(bot.load_telegram_token(), "file-token")
                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "file-token")
                self.assertNotIn("BOT_BACKEND", os.environ)
                os.environ["TELEGRAM_BOT_TOKEN"] = "process-token"
                self.assertEqual(bot.load_telegram_token(), "process-token")


if __name__ == "__main__":
    unittest.main()
