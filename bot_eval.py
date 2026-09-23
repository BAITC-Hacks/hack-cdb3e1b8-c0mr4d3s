"""Bot-owned bridge: the real Agent on the organizer's local mock environment.

Uses the same scoring procedure as local_eval.py. No scoring formula lives here.
The evaluation teammate can replace this bridge with demo_eval.run_demo later.
"""

from pathlib import Path

import pandas as pd

from agent import Agent
from mock_environment import make_mock_env, _mock_fallback, _mock_impact_model
from scoring_core import MAX_CAMPAIGNS, sanitize_campaigns, score_campaigns

ROOT = Path(__file__).resolve().parent
FILTER_COLUMNS = ["filter_arpu_segment", "filter_data_segment",
                  "filter_call_segment", "filter_current_tariff", "explicit_ids"]


def run_demo(seed: int = 42) -> dict:
    env, internals = make_mock_env(
        seed=seed, data_dir=ROOT / "data", profile_path=ROOT / "customer_profile.csv",
    )
    error = None
    try:
        campaigns = Agent().act(env)
    except Exception as exc:
        # Keep already executed pilots in scoring even if the agent fails.
        # External exception messages may contain keys; expose only the type.
        error = f"Agent.act завершился ошибкой ({type(exc).__name__})"
        campaigns = []
    campaigns = sanitize_campaigns(campaigns, env.tariffs)[:MAX_CAMPAIGNS]
    pilots = internals.executed_pilot_campaigns()
    strategy = pd.DataFrame(pilots + campaigns)
    for column in FILTER_COLUMNS:
        if column not in strategy.columns:
            strategy[column] = None
    profile = env.customer_profile
    model = _mock_impact_model(pd.read_csv(ROOT / "data" / "change_tariff.csv"))
    score = score_campaigns(
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
