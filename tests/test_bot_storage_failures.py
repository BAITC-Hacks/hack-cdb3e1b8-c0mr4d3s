"""Offline SQLite failure and process-restart checks of the real bot handlers."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class StorageFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from scripts import bot
        self.bot = bot
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "runs.sqlite3"
        self.store = bot.RunStore(self.db_path)
        self.runner = Mock(side_effect=bot.mock_run_demo)
        self.handlers = bot.BotHandlers(self.store, "mock", self.runner)
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
            effective_message=SimpleNamespace(reply_text=AsyncMock()))
        self.context = SimpleNamespace(args=[])

    def replies(self):
        return "\n".join(call.args[0] for call in
                         self.update.effective_message.reply_text.await_args_list)

    async def test_insert_failure_does_not_run_agent_or_claim_success(self):
        with patch.object(self.store, "start", side_effect=sqlite3.OperationalError("database is full")):
            with self.assertRaises(sqlite3.OperationalError) as failure:
                await self.handlers.run(self.update, self.context)
        await self.bot.on_error(self.update, SimpleNamespace(error=failure.exception))
        self.runner.assert_not_called()
        self.assertEqual(self.store.recent(123), [])
        self.assertIn("Не удалось обработать команду", self.replies())
        self.assertNotIn("Статус оценки", self.replies())
        self.assertFalse(self.handlers.run_lock.locked())
        await self.handlers.run(self.update, self.context)
        self.assertEqual(self.store.recent(123)[0]["state"], "done")

    async def test_finish_failure_remains_unfinished_until_restart_recovery(self):
        with patch.object(self.store, "finish", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError) as failure:
                await self.handlers.run(self.update, self.context)
        await self.bot.on_error(self.update, SimpleNamespace(error=failure.exception))
        self.runner.assert_called_once_with(42)
        row = self.store.recent(123)[0]
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["metrics_json"])
        self.assertNotIn("Статус оценки", self.replies())
        self.assertFalse(self.handlers.run_lock.locked())
        reopened = self.bot.RunStore(self.db_path)
        reopened.recover_interrupted()
        row = reopened.recent(123)[0]
        self.assertEqual(row["state"], "error")
        self.assertIn("прерван", row["error"])
        await self.handlers.run(self.update, self.context)
        self.assertEqual(self.store.recent(123)[0]["state"], "done")

    async def test_new_python_process_restores_identical_saved_record(self):
        await self.handlers.run(self.update, self.context)
        expected = self.store.recent(123)[0]
        program = (
            "import json,sys; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); from scripts import bot; "
            "print(json.dumps(bot.RunStore(Path(sys.argv[2])).recent(123)[0],ensure_ascii=False))"
        )
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", "-c", program, str(self.bot.ROOT), str(self.db_path)],
            cwd=self.temp.name, capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)
        self.runner.assert_called_once()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    project = args.project.resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
