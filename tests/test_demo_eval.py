"""Offline contract and bot integration tests against the canonical scripts.local_eval.run_demo.

Run: python tests/test_demo_eval.py --project PATH
Only Telegram delivery and the optional Gemini client are replaced in tests.
"""

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class DemoEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts import local_eval
        cls.demo = local_eval

    def setUp(self):
        from scripts.agent import Agent
        from scripts.config import AgentConfig
        self.agent = Mock(wraps=Agent(settings=AgentConfig()))
        self.enterContext(patch.object(self.demo, "Agent", return_value=self.agent))
        self.provider = self.enterContext(patch("scripts.agent.GeminiClient.generate_json",
            side_effect=AssertionError("External API forbidden in this suite")))
        self.addCleanup(self.provider.assert_not_called)

    @contextmanager
    def observe(self):
        captured = {}
        original_make = self.demo.make_mock_env
        original_score = self.demo.score_campaigns

        def make(**kwargs):
            captured["env"], captured["internals"] = original_make(**kwargs)
            return captured["env"], captured["internals"]

        def score(*args, **kwargs):
            captured["frame"] = args[0].copy(deep=True)
            captured["profile"] = args[1]
            captured["metrics"] = original_score(*args, **kwargs)
            return captured["metrics"]

        with patch.object(self.demo, "make_mock_env", side_effect=make) as factory, \
             patch.object(self.demo, "score_campaigns", side_effect=score) as scorer:
            yield captured, factory, scorer

    def test_same_run_pilot_ids_and_final_campaigns_reach_original_scorer(self):
        import pandas as pd

        with self.observe() as (captured, factory, scorer):
            result = self.demo.run_demo(42)
        factory.assert_called_once()
        scorer.assert_called_once()
        self.agent.act.assert_called_once_with(captured["env"])
        self.assertIs(captured["profile"], captured["env"].customer_profile)
        pilots = captured["internals"].executed_pilot_campaigns()
        self.assertEqual(result["n_pilots"], len(pilots))
        frame = captured["frame"]
        self.assertEqual(len(frame), len(pilots) + len(result["campaigns"]))
        for i, pilot in enumerate(pilots):
            for key, value in pilot.items():
                # DataFrame construction normalizes absent filters to NaN;
                # the scorer treats these and None as the same missing filter.
                if value is None:
                    self.assertTrue(pd.isna(frame.iloc[i][key]), f"pilot {i}: {key}")
                else:
                    self.assertEqual(frame.iloc[i][key], value, f"pilot {i}: {key}")
        for i, campaign in enumerate(result["campaigns"], start=len(pilots)):
            for key, value in campaign.items():
                self.assertEqual(frame.iloc[i][key], value, f"final {i}: {key}")
        for key, value in result["metrics"].items():
            self.assertEqual(value, captured["metrics"][key])
        json.dumps(result, allow_nan=False)

    def test_multiple_seeds_match_official_evaluator(self):
        from scripts.agent import Agent
        from scripts.config import AgentConfig
        from scripts.local_eval import evaluate_agent
        for seed in (0, 3, 42):
            with self.subTest(seed=seed):
                result = self.demo.run_demo(seed)
                expected = evaluate_agent(Agent(settings=AgentConfig()), seed=seed, verbose=False)
                self.assertEqual(result["seed"], seed)
                self.assertEqual(result["n_pilots"], expected["n_pilots"])
                self.assertEqual(result["metrics"], {key: expected["metrics"][key] for key in result["metrics"]})
        self.assertEqual(self.agent.act.call_count, 3)

    def test_requested_seed_controls_real_pilot_samples(self):
        observations = []
        for seed in (42, 42, 7):
            with self.observe() as (captured, factory, _):
                result = self.demo.run_demo(seed)
            self.assertEqual(factory.call_args.kwargs["seed"], seed)
            observations.append((result, captured["internals"].executed_pilot_campaigns()))
        self.assertEqual(observations[0], observations[1])
        self.assertNotEqual(observations[0][1][0]["explicit_ids"], observations[2][1][0]["explicit_ids"])

    def test_paths_work_outside_project_without_changing_cwd(self):
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as folder:
                os.chdir(folder)
                result = self.demo.run_demo()
                self.assertEqual(Path.cwd(), Path(folder))
                self.assertEqual(result["seed"], 42)
                self.assertTrue(result["campaigns"])
                os.chdir(previous)
        finally:
            os.chdir(previous)

    def test_crash_keeps_actual_pilot_contacts_and_safe_error(self):
        def fail(env):
            env.run_pilot(target_tariff="tariff_8", channel="sms", n_customers=50)
            raise RuntimeError("private upstream credential details")

        self.agent.act.side_effect = fail
        result = self.demo.run_demo()
        self.agent.act.assert_called_once()
        self.assertEqual(result["n_pilots"], 1)
        self.assertEqual(result["campaigns"], [])
        self.assertEqual(result["metrics"]["total_contacts"], 50)
        self.assertEqual(result["metrics"]["total_cost"], 200)
        self.assertIn("RuntimeError", result["error"])
        self.assertNotIn("credential", json.dumps(result))

    def test_no_campaigns_returns_official_zero_score(self):
        self.agent.act.side_effect = lambda env: []
        result = self.demo.run_demo()
        self.assertEqual(result["campaigns"], [])
        self.assertEqual(result["n_pilots"], 0)
        self.assertEqual(result["metrics"]["total_contacts"], 0)
        self.assertEqual(result["metrics"]["total_cost"], 0)
        self.assertEqual(result["metrics"]["status"], "FAIL")

    def test_zero_cost_score_becomes_strict_json_at_bot_boundary(self):
        from scripts.bot import validate_result
        self.agent.act.side_effect = lambda env: []
        raw = self.demo.run_demo(42)
        self.assertEqual(raw["metrics"]["roi"], float("inf"))
        normalized = validate_result(raw, 42)
        json.dumps(normalized, allow_nan=False)
        self.assertEqual(normalized["metrics"]["total_cost"], 0)
        self.assertEqual(normalized["metrics"]["status"], "FAIL")
        self.agent.act.assert_called_once()

    def test_scoring_failure_propagates_to_bot_error_handling(self):
        with patch.object(self.demo, "score_campaigns", side_effect=OSError("scoring failed")):
            with self.assertRaises(OSError):
                self.demo.run_demo()
        self.agent.act.assert_called_once()

    def test_existing_bot_commands_use_new_module_and_saved_result(self):
        from scripts import bot

        async def commands():
            with tempfile.TemporaryDirectory() as folder:
                db = Path(folder) / "runs.sqlite3"
                store = bot.RunStore(db)
                runner = Mock(wraps=bot.load_runner("real"))
                handlers = bot.BotHandlers(store, "real", runner)
                update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
                    effective_message=SimpleNamespace(reply_text=AsyncMock()))
                context = SimpleNamespace(args=[])
                await handlers.run(update, context)
                row = store.recent(123)[0]
                self.assertEqual(row["state"], "done", row["error"])
                restarted = bot.BotHandlers(bot.RunStore(db), "real", runner)
                await restarted.last(update, context)
                await restarted.history(update, context)
                runner.assert_called_once_with(42)
                self.agent.act.assert_called_once()
                self.assertEqual(bot.RunStore(db).recent(123)[0], row)
                messages = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
                self.assertIn(bot.format_run(row), "".join(messages))

        asyncio.run(commands())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    project = args.project.resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
