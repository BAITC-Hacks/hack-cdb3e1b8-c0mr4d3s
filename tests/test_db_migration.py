"""Upgrade existing SQLite history without overwriting user-selected databases."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import bot, config


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(patch.object(bot, "ROOT", self.root))
        self.enterContext(patch.object(config, "ROOT", self.root))
        self.legacy = self.root / "bot_data" / "runs.sqlite3"
        self.current = self.root / config.BotConfig().db_path

    def save_run(self, store, chat_id=123):
        run_id = store.start(chat_id, 42, "mock")
        bot.execute_run(store, run_id, bot.mock_run_demo, 42)
        return run_id

    def test_default_upgrade_preserves_history_and_next_run_id(self):
        old_store = bot.RunStore(self.legacy)
        first_id = self.save_run(old_store)
        expected = dict(old_store.recent(123)[0])
        (self.root / "config.json").write_text(json.dumps({"bot": {"backend": "mock"}}))

        settings = config.load_config()
        store = bot.RunStore(self.root / settings.bot.db_path)

        self.assertEqual(dict(store.recent(123)[0]), expected)
        self.assertTrue(self.legacy.exists())
        next_id = self.save_run(store)
        self.assertEqual(next_id, first_id + 1)
        self.assertEqual(len(old_store.recent(123)), 1)
        self.assertEqual(len(bot.RunStore(self.current).recent(123)), 2)

    def test_existing_new_database_is_not_replaced(self):
        current_store = bot.RunStore(self.current)
        self.save_run(current_store, chat_id=456)
        expected = dict(current_store.recent(456)[0])
        self.save_run(bot.RunStore(self.legacy), chat_id=123)

        reopened = bot.RunStore(self.current)

        self.assertEqual(dict(reopened.recent(456)[0]), expected)
        self.assertEqual(reopened.recent(123), [])

    def test_custom_database_does_not_import_legacy_history(self):
        self.save_run(bot.RunStore(self.legacy))

        custom = bot.RunStore(self.root / "custom.sqlite3")

        self.assertEqual(custom.recent(123), [])
        self.assertFalse(self.current.exists())
        self.assertEqual(len(bot.RunStore(self.legacy).recent(123)), 1)

    def test_backup_includes_committed_wal_records(self):
        old_store = bot.RunStore(self.legacy)
        run_id = self.save_run(old_store)
        with closing(old_store.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("UPDATE bot_runs SET error='committed WAL update', state='error' WHERE id=?",
                               (run_id,))
            connection.commit()
            wal = Path(str(self.legacy) + "-wal")
            self.assertGreater(wal.stat().st_size, 0)
            expected = dict(connection.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())

            migrated = bot.RunStore(self.current)

            self.assertEqual(dict(migrated.recent(123)[0]), expected)

    def test_failed_backup_leaves_source_and_no_empty_destination(self):
        self.legacy.parent.mkdir(parents=True)
        original = b"not a SQLite database"
        self.legacy.write_bytes(original)

        with self.assertRaises(sqlite3.DatabaseError):
            bot.RunStore(self.current)

        self.assertEqual(self.legacy.read_bytes(), original)
        self.assertFalse(self.current.exists())
        self.assertEqual(list(self.current.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
