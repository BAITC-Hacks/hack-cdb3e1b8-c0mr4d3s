"""Regression tests for the checker itself, using the supplied mock data.

Set QA_PROJECT to a participant-kit copy. These tests intentionally inject bad
agents/demo implementations to prove QA detects failures, not to certify a bot.
"""
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from tests import qa_check


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_cwd = Path.cwd()
        cls.project = Path(os.environ.get("QA_PROJECT", Path(__file__).resolve().parents[1])).resolve()
        os.chdir(cls.project)
        sys.path.insert(0, str(cls.project))
        # Load dependencies before patch.dict restores sys.modules, avoiding
        # repeated imports of NumPy's native extension modules between tests.
        __import__("scripts.local_eval")

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.project))
        os.chdir(cls.old_cwd)

    @staticmethod
    def good(env):
        sizes = env.customer_profile.groupby(["current_tariff", "arpu_segment"]).size()
        current, segment = sizes[(sizes >= 10) & (sizes <= 1000)].index[0]
        target = next(t for t in env.tariffs["tariff_plan_code"] if t != current)
        campaign = {"filter_current_tariff": current, "filter_arpu_segment": segment,
                    "target_tariff": target, "channel": "push"}
        env.run_pilot(**campaign, n_customers=10)
        return [campaign]

    def audit(self, action):
        fake = types.ModuleType("qa_fixture_agent")
        fake.Agent = type("Agent", (), {"act": lambda instance, env: action(env)})
        with patch.dict(sys.modules, {"qa_fixture_agent": fake}):
            return qa_check.check_agent("qa_fixture_agent", 42)

    def test_valid_agent(self):
        result = self.audit(self.good)
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["n_pilots"])

    def test_empty_answer(self):
        self.assertTrue(any("1..10 campaigns" in e for e in self.audit(lambda env: [])["errors"]))

    def test_exception_hidden_by_local_eval(self):
        def crash(env):
            self.good(env)
            raise RuntimeError("fixture failure")
        self.assertTrue(any("Agent.act raised" in e for e in self.audit(crash)["errors"]))

    def test_invalid_tariff_dropped_by_local_eval(self):
        def bad(env):
            result = self.good(env)
            result[0]["target_tariff"] = "nonexistent_tariff"
            return result
        self.assertTrue(any("Invalid campaign" in e for e in self.audit(bad)["errors"]))

    def test_duplicate_segments(self):
        self.assertTrue(any("identical final segment" in e for e in self.audit(lambda env: self.good(env) * 2)["errors"]))

    def test_automatic_budget_trimming(self):
        def overbudget(env):
            self.good(env)
            return [{"target_tariff": "tariff_8", "channel": "call"}]
        self.assertTrue(any("trimming" in e for e in self.audit(overbudget)["errors"]))

    def demo_audit(self, repeats=1, fake_metric=False, omit_pilots=False, bad_json=False):
        from scripts import agent, local_eval
        fake_agent = types.ModuleType("agent")
        fake_agent.Agent = type("Agent", (), {"act": lambda instance, env: self.good(env)})
        fake_demo = types.ModuleType("demo_eval")

        def run_demo(seed=42):
            import pandas as pd
            from scripts import agent
            from scripts.environment import make_mock_env, _mock_impact_model, _mock_fallback
            from scripts.scoring_core import score_campaigns
            for _ in range(repeats):
                env, internal = make_mock_env(seed=seed)
                campaigns = agent.Agent().act(env)
            pilots = internal.executed_pilot_campaigns()
            strategy = pd.DataFrame(([] if omit_pilots else pilots) + campaigns)
            result = score_campaigns(strategy, env.customer_profile,
                                     _mock_impact_model(pd.read_csv("data/change_tariff.csv")), env.tariffs,
                                     env.customer_profile["predicted_arpu"].sum(), _mock_fallback)
            metrics = {key: result[key] for key in ("net_arpu_gain", "total_cost", "total_contacts", "status")}
            if fake_metric:
                metrics["total_cost"] += 123
            value = {"seed": seed, "campaigns": campaigns, "n_pilots": len(pilots), "metrics": metrics}
            if bad_json:
                value["metrics"]["net_arpu_gain"] = float("nan")
            return value

        fake_demo.run_demo = run_demo
        with patch.object(agent, "Agent", fake_agent.Agent), patch.object(local_eval, "run_demo", fake_demo.run_demo):
            return qa_check.check_demo()

    def test_demo_single_call_uses_real_scoring(self):
        self.assertEqual([], self.demo_audit()["errors"])

    def test_demo_double_agent_call(self):
        self.assertTrue(any("once" in e for e in self.demo_audit(repeats=2)["errors"]))

    def test_demo_metric_mismatch(self):
        self.assertTrue(any("Metric total_cost" in e for e in self.demo_audit(fake_metric=True)["errors"]))

    def test_demo_omitted_pilots(self):
        self.assertTrue(any("pilots + finals" in e for e in self.demo_audit(omit_pilots=True)["errors"]))

    def test_demo_non_json_value(self):
        self.assertTrue(any("strict JSON" in e for e in self.demo_audit(bad_json=True)["errors"]))


if __name__ == "__main__":
    unittest.main()
