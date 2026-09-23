"""Run one agent on the local mock and expose the agreed demo result.

The scoring procedure follows the organizer's local_eval.py and the team's
bot_eval bridge. This module does not import Telegram or SQLite.
"""

from pathlib import Path

import pandas as pd

from agent import Agent
from mock_environment import make_mock_env, _mock_fallback, _mock_impact_model
import scoring_core

ROOT = Path(__file__).resolve().parent
FILTER_COLUMNS = ["filter_arpu_segment", "filter_data_segment",
                  "filter_call_segment", "filter_current_tariff", "explicit_ids"]


def run_demo(seed: int = 42) -> dict:
    """Return seed, final campaigns, pilot count and organizer scoring metrics.

    A failed Agent.act returns an additional safe ``error`` string while the
    score still includes already executed pilots. Setup/scoring failures raise
    to the caller, which owns persistence and user-facing error handling.
    """
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    env, internals = make_mock_env(
        seed=seed, data_dir=ROOT / "data", profile_path=ROOT / "customer_profile.csv",
    )
    error = None
    try:
        campaigns = Agent().act(env)
    except Exception as exc:
        # Preserve paid pilots, without exposing external exception messages.
        error = f"Agent.act завершился ошибкой ({type(exc).__name__})"
        campaigns = []
    campaigns = scoring_core.sanitize_campaigns(campaigns, env.tariffs)[:scoring_core.MAX_CAMPAIGNS]
    pilots = internals.executed_pilot_campaigns()
    strategy = pd.DataFrame(pilots + campaigns)
    for column in FILTER_COLUMNS:
        if column not in strategy.columns:
            strategy[column] = None
    profile = env.customer_profile
    model = _mock_impact_model(pd.read_csv(ROOT / "data" / "change_tariff.csv"))
    score = scoring_core.score_campaigns(
        strategy, profile, model, env.tariffs, profile["predicted_arpu"].sum(),
        _mock_fallback, team_id="telegram",
    )
    result = {
        "seed": seed,
        "campaigns": campaigns,
        "n_pilots": len(pilots),
        "metrics": {key: score[key] for key in
                    ("net_arpu_gain", "total_cost", "total_contacts", "status")},
    }
    if error:
        result["error"] = error
    return result
