"""Offline stress checks of the real agent; no external API calls or config reads.

python qa/test_agent_resilience.py --project .
Reduced resource scenarios are robustness tests, not extra contest rules.
"""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class AgentResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pandas as pd
        from mock_environment import _mock_impact_model
        cls.profile = pd.read_csv("customer_profile.csv")
        cls.tariffs = pd.read_csv("data/dict_tariff.csv")
        cls.model = _mock_impact_model(pd.read_csv("data/change_tariff.csv"))

    def run_agent(self, *, budget=100_000, contacts=15_000, lift=None,
                  fail_first=False, strategy=None):
        from agent import Agent
        from config import AgentConfig
        from environment import make_environment
        from mock_environment import CHANNELS, _mock_fallback

        env, internals = make_environment(self.profile, self.model, self.tariffs,
                                          CHANNELS, budget, contacts, _mock_fallback, seed=42)
        original_pilot = env.run_pilot
        requests = []

        def pilot(*args, **kwargs):
            requests.append(dict(kwargs))
            if fail_first and len(requests) == 1:
                raise RuntimeError("Injected single pilot failure")
            result = original_pilot(*args, **kwargs)
            if lift is not None:
                # Change only public observations; the actual pilot still consumes
                # real mock budget and contacts and is retained in pilot_history.
                result["observed_lift_ratio"] = lift
                # Only this test harness receives organizer bookkeeping. Keep the
                # reported total consistent without exposing internals to Agent.
                ids = internals.executed_pilot_campaigns()[-1]["explicit_ids"]
                arpu = env.customer_profile.loc[env.customer_profile["ID_NUMBER"].isin(ids), "predicted_arpu"].sum()
                result["observed_lift_total"] = float(lift * arpu)
            return result

        env.run_pilot = pilot
        settings = AgentConfig()  # Gemini is disabled; do not read private config.json.
        if strategy is not None:
            settings = replace(settings, strategy=strategy)
        with patch("llm.GeminiClient.generate_json", side_effect=AssertionError("Network forbidden in offline tests")) as provider:
            campaigns = Agent(settings=settings).act(env)
        provider.assert_not_called()
        self.assert_plan_fits(campaigns, env)
        self.assertGreater(len(env.pilot_history), 0)
        self.assertLessEqual(len(env.pilot_history), 20)
        for request in requests:
            self.assertGreaterEqual(request["n_customers"], 10)
            self.assertLessEqual(request["n_customers"], 200)
        self.assertAlmostEqual(sum(p["cost"] for p in env.pilot_history), budget - env.remaining_budget)
        self.assertEqual(sum(p["n_customers"] for p in env.pilot_history), contacts - env.remaining_contacts)
        return campaigns, env, requests

    def assert_plan_fits(self, campaigns, env):
        import pandas as pd
        from scoring_core import apply_filters, validate_strategy
        self.assertIsInstance(campaigns, list)
        self.assertGreaterEqual(len(campaigns), 1)
        self.assertLessEqual(len(campaigns), 10)
        validate_strategy(pd.DataFrame(campaigns), env.tariffs)
        total_contacts, total_cost = 0, 0
        seen = set()
        for campaign in campaigns:
            audience = apply_filters(env.customer_profile, pd.Series(campaign))
            ids = set(audience["ID_NUMBER"])
            self.assertGreater(len(ids), 0)
            self.assertLessEqual(len(ids), 5000)
            self.assertFalse(seen & ids, "Final segments overlap")
            seen.update(ids)
            total_contacts += len(ids)
            total_cost += len(ids) * env.channels[campaign["channel"]]["cost_per_contact"]
        self.assertLessEqual(total_contacts, env.remaining_contacts)
        self.assertLessEqual(total_cost, env.remaining_budget)

    def test_all_negative_pilots_still_produce_a_valid_plan(self):
        campaigns, _, _ = self.run_agent(lift=-1.0)
        self.assertTrue(all(c["channel"] == "push" for c in campaigns))

    def test_pilot_observations_change_the_final_decision(self):
        positive, _, _ = self.run_agent(lift=1.0)
        negative, _, _ = self.run_agent(lift=-1.0)
        self.assertNotEqual(positive, negative)
        self.assertTrue(any(c["channel"] == "sms" for c in positive))

    def test_single_failed_pilot_does_not_stop_exploration(self):
        _, env, requests = self.run_agent(fail_first=True)
        self.assertGreater(len(requests), 1)
        self.assertEqual(len(requests) - 1, len(env.pilot_history))

    def test_non_finite_pilot_results_use_fallback(self):
        for lift in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(lift=lift):
                campaigns, _, _ = self.run_agent(lift=lift)
                self.assertTrue(all(c["channel"] == "push" for c in campaigns))

    def test_small_budget_accounts_for_pilot_spending(self):
        campaigns, env, _ = self.run_agent(budget=600)
        self.assertEqual(0, env.remaining_budget)
        self.assertTrue(all(c["channel"] == "push" for c in campaigns))

    def test_small_contact_limit_reserves_room_for_final_campaigns(self):
        self.run_agent(contacts=2000)

    def test_allowed_pilot_size_boundaries(self):
        from config import StrategyConfig
        for size in (10, 200):
            with self.subTest(pilot_size=size):
                self.run_agent(strategy=StrategyConfig(pilot_size=size, max_pilots=20))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    project = args.project.resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
