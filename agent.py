"""History-ranked hypotheses, SMS pilots, and conservative campaign selection.

Optional Gemini prioritization is configured through config.json:
set gemini.enabled to true and fill gemini.api_key.
Keep it disabled for reproducible offline evaluation/submission.
"""

import json
from math import isfinite, sqrt
from pathlib import Path

import pandas as pd

from config import AgentConfig, load_config
from llm import JsonLLMClient, create_llm_client
from logging_config import configure_logging, logger


def _build_candidates(env, settings: AgentConfig) -> list[dict]:
    history_config = settings.history
    strategy = settings.strategy
    cells = (
        env.customer_profile.groupby(["current_tariff", "arpu_segment"], observed=True)
        .agg(size=("ID_NUMBER", "size"), mean_arpu=("predicted_arpu", "mean"))
        .reset_index()
    )
    cells = cells[cells["size"].between(strategy.min_segment_size, strategy.max_segment_size)]

    history = pd.read_csv(Path(__file__).resolve().parent / history_config.relative_path)
    history = history[history["AVG_ARPU_PREV_3M"] >= history_config.min_arpu].dropna(
        subset=["AVG_ARPU_NEXT_3M", "tariff_plan_code_from", "tariff_plan_code_to"]
    ).copy()
    history["arpu_segment"] = pd.cut(
        history["AVG_ARPU_PREV_3M"],
        bins=(float("-inf"), *history_config.arpu_thresholds, float("inf")),
        labels=history_config.arpu_labels,
    )
    history["relative_change"] = (
        (history["AVG_ARPU_NEXT_3M"] - history["AVG_ARPU_PREV_3M"])
        / history["AVG_ARPU_PREV_3M"]
    ).clip(*history_config.lift_bounds)
    transitions = (
        history.groupby(
            ["tariff_plan_code_from", "arpu_segment", "tariff_plan_code_to"],
            observed=True,
        )
        .agg(history_size=("relative_change", "size"), mean_change=("relative_change", "mean"))
        .reset_index()
    )
    totals = transitions.groupby(
        ["tariff_plan_code_from", "arpu_segment"], observed=True
    )["history_size"].transform("sum")
    # Transition share is only a prior proxy for conversion; pilots update it.
    transitions["prior_ratio"] = transitions["mean_change"] * (
        transitions["history_size"] / totals
        * env.channels["sms"]["conversion_multiplier"]
    ).clip(upper=1)
    transitions = transitions[
        (transitions["history_size"] >= history_config.min_samples)
        & (transitions["tariff_plan_code_from"] != transitions["tariff_plan_code_to"])
        & transitions["tariff_plan_code_to"].isin(env.tariffs["tariff_plan_code"])
    ].rename(columns={
        "tariff_plan_code_from": "current_tariff",
        "tariff_plan_code_to": "target_tariff",
    })
    candidates = cells.merge(transitions, on=["current_tariff", "arpu_segment"])
    candidates["prior_net"] = candidates["size"] * (
        candidates["mean_arpu"] * candidates["prior_ratio"]
        - env.channels["sms"]["cost_per_contact"]
    )
    candidates = (
        candidates.sort_values(
            ["prior_net", "current_tariff", "arpu_segment", "target_tariff"],
            ascending=[False, True, True, True],
        )
        .drop_duplicates(["current_tariff", "arpu_segment"])
    )
    return candidates.to_dict("records")


def _prioritize_candidates(
    candidates: list[dict], sms_cost: float, settings: AgentConfig,
    llm_client: JsonLLMClient | None,
) -> list[dict]:
    strategy = settings.strategy
    default = candidates[:strategy.max_pilots]
    if llm_client is None or len(candidates) <= strategy.max_pilots:
        return default

    # Send aggregate hypotheses only; the model may select IDs, not invent offers.
    summaries = [{
        "id": index,
        "current_tariff": candidate["current_tariff"],
        "arpu_segment": candidate["arpu_segment"],
        "target_tariff": candidate["target_tariff"],
        "customers": candidate["size"],
        "mean_arpu": candidate["mean_arpu"],
        "historical_samples": candidate["history_size"],
        "prior_sms_lift_ratio": candidate["prior_ratio"],
        "prior_net": candidate["prior_net"],
    } for index, candidate in enumerate(candidates)]
    try:
        prompt = settings.gemini.ranking_prompt.format(
            max_pilots=strategy.max_pilots,
            pilot_size=strategy.pilot_size,
            sms_cost=sms_cost,
            candidates_json=json.dumps(summaries, allow_nan=False),
        )
        ids = llm_client.generate_json(prompt, {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": strategy.max_pilots,
            "maxItems": strategy.max_pilots,
        })
        if (
            not isinstance(ids, list)
            or len(ids) != strategy.max_pilots
            or any(type(index) is not int or not 0 <= index < len(candidates) for index in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("Invalid candidate IDs")
        return [candidates[index] for index in ids]
    except Exception as exc:
        logger.warning(
            "LLM unavailable or invalid response (%s); using historical ranking.",
            type(exc).__name__,
        )
        return default


def _campaign(candidate: dict, channel: str) -> dict:
    return {
        "campaign_name": (
            f"{channel}_{candidate['current_tariff']}_"
            f"{candidate['arpu_segment']}_{candidate['target_tariff']}"
        ),
        "filter_current_tariff": candidate["current_tariff"],
        "filter_arpu_segment": candidate["arpu_segment"],
        "target_tariff": candidate["target_tariff"],
        "channel": channel,
    }


class Agent:
    def __init__(
        self, settings: AgentConfig | None = None,
        llm_client: JsonLLMClient | None = None,
    ):
        self.settings = settings if settings is not None else load_config()
        configure_logging(self.settings.logging)
        self.llm_client = (
            llm_client if llm_client is not None else create_llm_client(self.settings.gemini)
        )

    def act(self, env) -> list[dict]:
        strategy = self.settings.strategy
        sms_cost = env.channels["sms"]["cost_per_contact"]
        candidates = _prioritize_candidates(
            _build_candidates(env, self.settings), sms_cost, self.settings, self.llm_client
        )
        if not candidates:
            return []

        # Leave room for at least one full final segment, even with reduced limits.
        reserved_contacts = min(candidate["size"] for candidate in candidates)
        observed = []
        for candidate in candidates:
            if (
                env.pilots_left <= 0
                or env.remaining_contacts < strategy.pilot_size + reserved_contacts
                or env.remaining_budget < strategy.pilot_size * sms_cost
            ):
                break
            try:
                result = env.run_pilot(
                    target_tariff=candidate["target_tariff"],
                    channel="sms",
                    n_customers=strategy.pilot_size,
                    filter_current_tariff=candidate["current_tariff"],
                    filter_arpu_segment=candidate["arpu_segment"],
                )
            except (RuntimeError, ValueError):
                continue

            n = int(result["n_customers"])
            ratio = float(result["observed_lift_ratio"])
            if n <= 0 or not isfinite(ratio):
                continue
            posterior = (
                strategy.prior_weight * candidate["prior_ratio"] + n * ratio
            ) / (strategy.prior_weight + n)
            conservative_ratio = (
                posterior - strategy.uncertainty_penalty * strategy.per_customer_std / sqrt(n)
            )
            # Final filters include the pilot customers again: pay for all contacts,
            # but only previously uncontacted customers add lift for the same offer.
            new_customers = max(candidate["size"] - n, 0)
            net = (
                new_customers * candidate["mean_arpu"] * conservative_ratio
                - candidate["size"] * sms_cost
            )
            observed.append({
                **candidate,
                "pilot_size": n,
                "observed_ratio": ratio,
                "posterior_ratio": posterior,
                "net_per_contact": net / candidate["size"],
            })

        remaining_budget = env.remaining_budget
        remaining_contacts = env.remaining_contacts
        campaigns = []
        for candidate in sorted(observed, key=lambda row: -row["net_per_contact"]):
            cost = candidate["size"] * sms_cost
            if (
                candidate["net_per_contact"] <= 0
                or candidate["size"] > remaining_contacts
                or cost > remaining_budget
            ):
                continue
            campaigns.append(_campaign(candidate, "sms"))
            remaining_contacts -= candidate["size"]
            remaining_budget -= cost
            if len(campaigns) == strategy.max_campaigns:
                break

        if campaigns:
            return campaigns

        # A push fallback satisfies the required campaign output even when the
        # estimates are negative; it cannot guarantee a positive business result.
        fallback = [
            candidate for candidate in (observed or candidates)
            if candidate["size"] <= env.remaining_contacts
        ]
        if not fallback:
            fallback = [
                candidate for candidate in candidates
                if candidate["size"] <= env.remaining_contacts
            ]
        if not fallback:
            return []
        best = max(
            fallback,
            key=lambda row: row.get("posterior_ratio", row["prior_ratio"])
            * row["mean_arpu"],
        )
        return [_campaign(best, "push")]
