"""Offline regressions for rejected tokens and SDK credential leaks."""

import io
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import User
from telegram.error import InvalidToken, TimedOut

import bot
from config import AgentConfig, LoggingConfig
import logging_config


TOKEN = "123456789:" + "FakeTestToken" * 3


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        for name in ("tariff_agent", "telegram", "httpx", "httpcore"):
            logger = logging.getLogger(name)
            state = (logger.handlers[:], logger.level, logger.propagate)
            self.addCleanup(self.restore_logger, logger, state)
            logger.handlers = []
        self.enterContext(patch.object(logging_config, "_handler", None))
        self.enterContext(patch("sys.stderr", self.output))
        self.enterContext(patch("sys.stdout", io.StringIO()))
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}))
        self.enterContext(patch.object(bot, "load_config", return_value=AgentConfig()))

    @staticmethod
    def restore_logger(logger, state):
        logger.handlers, logger.level, logger.propagate = state

    def assert_no_secret(self):
        self.assertNotIn(TOKEN, self.output.getvalue())
        self.assertNotIn("FakeTestToken", self.output.getvalue())
        self.assertNotIn("Traceback", self.output.getvalue())

    def test_sdk_bootstrap_traceback_and_request_url_are_safe(self):
        logging_config.configure_logging(LoggingConfig())
        sdk_logger = logging.getLogger("telegram.ext._utils.networkloop")
        try:
            try:
                raise RuntimeError("Private upstream details")
            except RuntimeError as exc:
                raise InvalidToken(f"The token `{TOKEN}` was rejected by the server.") from exc
        except InvalidToken:
            # Simulate a traceback already formatted by another handler.
            record = sdk_logger.makeRecord(sdk_logger.name, logging.ERROR, __file__, 1,
                                           "Bootstrap failed", (), sys.exc_info())
            logging.Formatter().format(record)
            sdk_logger.handle(record)
        logging.getLogger("httpx").warning("Request https://api.telegram.org/bot%s/getMe", TOKEN)
        self.assert_no_secret()
        self.assertNotIn("Private upstream details", self.output.getvalue())
        self.assertIn("InvalidToken", self.output.getvalue())
        self.assertIn("[TOKEN]", self.output.getvalue())

    def test_repeated_setup_has_one_handler_and_preserves_root(self):
        root = logging.getLogger()
        original = (root.handlers[:], root.level)
        for _ in range(3):
            logging_config.configure_logging(LoggingConfig())
        for name in ("tariff_agent", "telegram", "httpx", "httpcore"):
            self.assertEqual(len(logging.getLogger(name).handlers), 1)
        self.assertEqual((root.handlers, root.level), original)

    def test_invalid_token_prevents_polling_and_database_creation(self):
        # Exercise real Bot.initialize(), which wraps Unauthorized in an
        # InvalidToken containing the token. Only the network call is mocked.
        with (patch.object(bot.sys, "argv", ["bot.py"]),
              patch("telegram.Bot.get_me", new_callable=AsyncMock,
                    side_effect=InvalidToken("Unauthorized")) as get_me,
              patch.object(bot, "build_application") as build,
              patch.object(bot, "RunStore") as store):
            self.assertEqual(bot.main(), 1)
        get_me.assert_awaited_once()
        build.assert_not_called()
        store.assert_not_called()
        self.assertIn("Telegram отклонил токен", self.output.getvalue())
        self.assert_no_secret()

    def test_connection_check_exits_without_polling(self):
        user = User(id=123, is_bot=True, first_name="Demo", username="demo_test_bot")
        with (patch.object(bot.sys, "argv", ["bot.py", "--check-telegram"]),
              patch("telegram.Bot._post", new_callable=AsyncMock,
                    return_value=user.to_dict()) as request,
              patch.object(bot, "build_application") as build,
              patch.object(bot, "RunStore") as store):
            self.assertEqual(bot.main(), 0)
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[0], "getMe")
        build.assert_not_called()
        store.assert_not_called()
        self.assertIn("https://t.me/demo_test_bot", self.output.getvalue())
        self.assert_no_secret()

    def test_successful_check_proceeds_to_polling(self):
        application = Mock()
        with (patch.object(bot.sys, "argv", ["bot.py"]),
              patch.object(bot, "check_telegram_connection", new_callable=AsyncMock,
                           return_value="demo_test_bot") as check,
              patch.object(bot, "build_application", return_value=application),
              patch.object(bot, "RunStore")):
            self.assertEqual(bot.main(), 0)
        check.assert_awaited_once_with(TOKEN)
        application.run_polling.assert_called_once()

    def test_network_failure_does_not_claim_token_is_invalid(self):
        with (patch.object(bot.sys, "argv", ["bot.py", "--check-telegram"]),
              patch("telegram.Bot.get_me", new_callable=AsyncMock, side_effect=TimedOut()),
              patch.object(bot, "build_application") as build):
            self.assertEqual(bot.main(), 1)
        build.assert_not_called()
        self.assertIn("Действительность токена не подтверждена", self.output.getvalue())
        self.assertNotIn("Telegram отклонил токен", self.output.getvalue())
        self.assert_no_secret()


if __name__ == "__main__":
    unittest.main()
