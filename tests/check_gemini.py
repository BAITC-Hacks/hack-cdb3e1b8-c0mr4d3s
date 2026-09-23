"""Observe real Gemini usage without exposing keys or raw provider errors.

Uses only aggregate candidate data through the project's existing adapter.
The live mode is opt-in; never treats a fallback as a successful API call.
"""
import argparse
import contextlib
import importlib.metadata
import io
import json
import logging
import os
from pathlib import Path
import sys
import time


def run_one(seed, mode):
    from dataclasses import replace
    from scripts import agent
    from scripts.config import load_config
    from scripts.agent import GeminiClient
    from scripts.local_eval import evaluate_agent
    from scripts.scoring_core import validate_strategy
    import pandas as pd

    settings = load_config()
    candidate_count = None
    records = []
    original_prioritize = agent._prioritize_candidates
    errors = []

    class ObservedClient:
        def generate_json(self, prompt, schema):
            started = time.monotonic()
            record = {"completed": False, "ids_valid": False}
            records.append(record)
            try:
                if mode == "timeout":
                    raise TimeoutError("Injected test failure")
                if mode == "invalid_json":
                    raise json.JSONDecodeError("Injected test failure", "?", 0)
                if mode == "invalid_ids":
                    value = [-1] * settings.strategy.max_pilots
                else:
                    value = GeminiClient(settings.gemini).generate_json(prompt, schema)
                record["completed"] = True
                record["ids_valid"] = (
                    isinstance(value, list)
                    and len(value) == settings.strategy.max_pilots
                    and all(type(i) is int and 0 <= i < candidate_count for i in value)
                    and len(set(value)) == len(value)
                )
                if record["ids_valid"]:
                    record["selected_ids"] = value
                return value
            except Exception as exc:
                # Do not print str(exc), URL, headers, body or configuration.
                record["error_type"] = type(exc).__name__
                code = getattr(exc, "code", None)
                record["http_status"] = code if isinstance(code, int) else None
                payload = getattr(exc, "response_json", None)
                if isinstance(payload, dict):
                    error = payload.get("error", payload)
                    allowed_statuses = {"INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED",
                                        "NOT_FOUND", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL"}
                    if isinstance(error, dict) and error.get("status") in allowed_statuses:
                        record["provider_status"] = error["status"]
                    reasons = []
                    for detail in error.get("details", []) if isinstance(error, dict) else []:
                        if isinstance(detail, dict) and detail.get("reason") in {
                            "API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED",
                            "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
                            "SERVICE_DISABLED", "BILLING_DISABLED", "CONSUMER_INVALID",
                        }:
                            reasons.append(detail["reason"])
                    if reasons:
                        record["provider_reasons"] = reasons
                raise
            finally:
                record["seconds"] = round(time.monotonic() - started, 3)

    def observe_prioritize(candidates, sms_cost, settings_arg, client):
        nonlocal candidate_count
        candidate_count = len(candidates)
        return original_prioritize(candidates, sms_cost, settings_arg, client)

    if mode == "offline":
        settings = replace(settings, gemini=replace(settings.gemini, enabled=False, api_key=""))
        client = None
    else:
        client = ObservedClient()

    class ObservedAgent:
        def act(self, env):
            self.env = env
            self.campaigns = agent.Agent(settings=settings, llm_client=client).act(env)
            return self.campaigns

    observed = ObservedAgent()
    started = time.monotonic()
    agent._prioritize_candidates = observe_prioritize
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = evaluate_agent(observed, seed=seed, verbose=False)["metrics"]
    finally:
        agent._prioritize_candidates = original_prioritize
        logging.disable(previous_disable)
    campaigns = getattr(observed, "campaigns", None)
    if not isinstance(campaigns, list) or not 1 <= len(campaigns) <= 10:
        errors.append("Missing/invalid final campaign list or Agent.act failed")
    else:
        try:
            validate_strategy(pd.DataFrame(campaigns), observed.env.tariffs)
        except Exception as exc:
            errors.append("Invalid final campaign: " + type(exc).__name__)
    if result is None:
        errors.append("No scoring result")
    elif any(c.get("capped_at_money_budget") or c.get("capped_at_reach_budget") for c in result["campaigns_detail"]):
        errors.append("Scorer had to trim budget/contacts")
    used = any(r["ids_valid"] for r in records)
    outcome = "OFFLINE" if mode == "offline" else ("MODEL_USED" if used else "FALLBACK" if records else "MODEL_NOT_CALLED")
    return {
        "seed": seed, "mode": mode, "model": settings.gemini.model,
        "candidate_count": candidate_count, "llm_outcome": outcome, "llm_calls": records,
        "n_pilots": len(observed.env.pilot_history), "n_campaigns": len(campaigns) if isinstance(campaigns, list) else 0,
        "campaigns": campaigns,
        "metrics": {k: result[k] for k in ("net_arpu_gain", "total_cost", "total_contacts", "status")} if result else None,
        "errors": errors, "seconds": round(time.monotonic() - started, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--mode", choices=["live", "offline", "timeout", "invalid_json", "invalid_ids"], required=True)
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    project, output = args.project.resolve(), args.report.resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    report = {"project": str(project), "runs": [], "versions": {
        p: importlib.metadata.version(p) for p in ("google-genai", "pandas", "numpy")}}
    for seed in args.seeds:
        result = run_one(seed, args.mode)
        report["runs"].append(result)
        print(json.dumps({k: result[k] for k in ("seed", "mode", "llm_outcome", "llm_calls", "metrics", "errors")}), flush=True)
        # Stop the batch after a provider failure so the operator can inspect it
        # before spending more quota (e.g. credentials vs transient HTTP 503).
        if args.mode == "live" and result["llm_outcome"] != "MODEL_USED":
            break
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return 1 if any(r["errors"] for r in report["runs"]) else 2 if args.mode == "live" and any(r["llm_outcome"] != "MODEL_USED" for r in report["runs"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
