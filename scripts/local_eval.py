"""
Локальная проверка агента — ваш основной инструмент во время работы.

    python -m scripts.local_eval              # один прогон
    python -m scripts.local_eval --runs 10    # 10 прогонов с разными seed + разброс
    python -m scripts.local_eval --submission # data/submission.csv для сдачи

Запускает вашего агента (файл agent.py рядом) на мок-среде, скорит результат
ТЕМ ЖЕ кодом, что и на судействе (scoring_core.py), и печатает полный отчёт:
пилоты, затраты, чистый результат.

ЧТО ЭТО ПОКАЗЫВАЕТ, А ЧТО НЕТ
  ✓ механику: лимиты, бюджет, дедупликацию, формулу — один в один как в финале;
  ✓ не падает ли агент, укладывается ли в лимиты, не жжёт ли бюджет впустую;
  ✗ НЕ показывает ваш будущий балл: эффекты в мок-среде выдуманные, на судействе
    они другие.

Поэтому не гонитесь за конкретным числом здесь. Смотрите на поведение агента:
разумно ли он тратит пилоты, не разоряется ли на дорогих каналах, устойчив ли к
неудачной серии пилотов. Прогон с --runs как раз про устойчивость: если между
seed результат скачет от плюса к минусу, на судействе вам просто не повезёт.
"""

import argparse

import pandas as pd

from .agent import Agent
from .config import DATA_DIR
from .environment import make_mock_env, _mock_fallback, _mock_impact_model, MAX_PILOTS
from .scoring_core import (
    MAX_CAMPAIGNS, TOTAL_BUDGET, MAX_TOTAL_CONTACTS,
    score_campaigns, print_result, sanitize_campaigns,
)

CAMPAIGN_FILTER_COLUMNS = ["filter_arpu_segment", "filter_data_segment",
                            "filter_call_segment", "filter_current_tariff"]
CAMPAIGN_COLUMNS = ["campaign_name", *CAMPAIGN_FILTER_COLUMNS, "target_tariff", "channel"]
SUBMISSION_SEED = 42


def evaluate_agent(agent, seed=None, verbose=True):
    env, internals = make_mock_env(seed=seed)

    error = None
    try:
        final_campaigns = agent.act(env)
    except Exception as exc:
        # Keep and score completed pilots without exposing external error details.
        error = f"Agent.act завершился ошибкой ({type(exc).__name__})"
        final_campaigns = []

    final_campaigns = sanitize_campaigns(final_campaigns, env.tariffs)[:MAX_CAMPAIGNS]
    pilots = internals.executed_pilot_campaigns()

    all_campaigns = pd.DataFrame(pilots + final_campaigns)
    for col in CAMPAIGN_FILTER_COLUMNS + ["explicit_ids"]:
        if col not in all_campaigns.columns:
            all_campaigns[col] = None

    profile = env.customer_profile
    baseline = profile["predicted_arpu"].sum()
    mock_model = _mock_impact_model(pd.read_csv(DATA_DIR / "change_tariff.csv"))
    score = score_campaigns(all_campaigns, profile, mock_model, env.tariffs,
                            baseline, _mock_fallback, team_id="local")
    result = {"seed": seed, "campaigns": final_campaigns,
              "n_pilots": len(pilots), "metrics": score}
    if error:
        result["error"] = error

    if verbose:
        if error:
            print(f"[!] {error}. Проведённые пилоты включены в оценку.")
        print_result(score)
        print(f"\nПилотов проведено: {len(pilots)} из {MAX_PILOTS}")
        print(f"Осталось бюджета: {env.remaining_budget:,.0f} из {TOTAL_BUDGET:,}")
        print(f"Осталось охвата:  {env.remaining_contacts:,} из {MAX_TOTAL_CONTACTS:,}")
    return result


def run_demo(seed: int = 42) -> dict:
    """The bot and CLI use the same evaluation path."""
    return evaluate_agent(Agent(), seed=seed, verbose=False)


def build_submission(agent, seed=SUBMISSION_SEED, **env_kwargs) -> pd.DataFrame:
    env, _ = make_mock_env(seed=seed, **env_kwargs)
    campaigns = agent.act(env) or []
    return pd.DataFrame(campaigns).reindex(columns=CAMPAIGN_COLUMNS)


def main():
    parser = argparse.ArgumentParser(description="Local agent evaluation and submission")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--runs", type=int, default=1, help="Number of seeds to evaluate")
    actions.add_argument("--submission", action="store_true", help="Write data/submission.csv")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")

    if args.submission:
        df = build_submission(Agent())
        path = DATA_DIR / "submission.csv"
        df.to_csv(path, index=False)
        print(f"Кампаний в стратегии: {len(df)}")
        print(df.to_string(index=False))
        print(f"\nСохранено: {path}")
        return

    if args.runs == 1:
        evaluate_agent(Agent(), seed=42)
        return

    results = []
    for seed in range(args.runs):
        res = evaluate_agent(Agent(), seed=seed, verbose=False)
        net = res["metrics"]["net_arpu_gain"]
        results.append(net)
        print(f"seed {seed:2}: чистый результат {net:>14,.0f}")

    s = pd.Series(results).dropna()
    print("\n--- устойчивость по прогонам ---")
    print(f"медиана: {s.median():,.0f}   минимум: {s.min():,.0f}   максимум: {s.max():,.0f}")
    print(f"прогонов в плюс: {(s > 0).sum()} из {len(s)}")
    if (s > 0).sum() not in (0, len(s)):
        print("⚠ Результат меняет знак между прогонами — агент неустойчив к неудачным пилотам.")


if __name__ == "__main__":
    main()
