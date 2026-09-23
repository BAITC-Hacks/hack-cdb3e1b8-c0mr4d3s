"""Strict mock QA. Run against a disposable project copy; no Telegram token needed.

python qa_check.py --project PATH --runs 10 --report qa_report.json
Uses the supplied local_eval/scoring_core; does not implement another score.
"""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def check_agent(module_name, seed):
    import pandas as pd
    from local_eval import evaluate_agent
    from scoring_core import apply_filters, validate_strategy

    errors, warnings = [], []

    class ObservedAgent:
        def act(self, env):
            self.env = env
            try:
                self.campaigns = importlib.import_module(module_name).Agent().act(env)
                return self.campaigns
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                raise

    observed = ObservedAgent()
    logs = io.StringIO()
    with contextlib.redirect_stdout(logs):
        result = evaluate_agent(observed, seed=seed, verbose=False)
    if hasattr(observed, "error"):
        errors.append(f"Agent.act raised: {observed.error}")
    campaigns = getattr(observed, "campaigns", None)
    env = observed.env
    if not isinstance(campaigns, list) or not 1 <= len(campaigns) <= 10:
        errors.append("Agent.act must return a list of 1..10 campaigns")
    elif not all(isinstance(c, dict) for c in campaigns):
        errors.append("Every campaign must be a dict")
    else:
        try:
            validate_strategy(pd.DataFrame(campaigns), env.tariffs)
        except (ValueError, TypeError) as exc:
            errors.append(f"Invalid campaign: {exc}")
        audiences = []
        for index, campaign in enumerate(campaigns):
            if "explicit_ids" in campaign:
                errors.append(f"Campaign {index}: explicit_ids is omitted by submission export")
            audience = frozenset(apply_filters(env.customer_profile, pd.Series(campaign))["ID_NUMBER"])
            if not audience:
                errors.append(f"Campaign {index}: empty segment")
            for previous, other in enumerate(audiences):
                if audience and audience == other:
                    errors.append(f"Campaigns {previous} and {index}: identical final segment (team requirement)")
                elif audience & other:
                    warnings.append(f"Campaigns {previous} and {index}: overlapping final segments")
            audiences.append(audience)
    pilots = len(env.pilot_history)
    if not 1 <= pilots <= 20:
        errors.append(f"Expected 1..20 pilots, got {pilots}")
    if env.pilots_left != 20 - pilots:
        errors.append("Pilot counter mismatch")
    if env.remaining_budget < 0 or env.remaining_contacts < 0:
        errors.append("Negative remaining pilot budget/contacts")
    if result is None:
        errors.append("Scorer produced no result")
    else:
        if not math.isfinite(float(result["net_arpu_gain"])):
            errors.append("Non-finite net_arpu_gain")
        if not 0 <= result["total_cost"] <= 100_000:
            errors.append("Total cost outside limits")
        if not 0 <= result["total_contacts"] <= 15_000:
            errors.append("Total contacts outside limits")
        for detail in result["campaigns_detail"]:
            if detail["capped_at_campaign_limit"]:
                warnings.append(f"{detail['name']}: capped at 5000 contacts (allowed by scorer)")
            if detail["capped_at_reach_budget"] or detail["capped_at_money_budget"]:
                errors.append(f"{detail['name']}: plan relies on scorer trimming total budget/contacts (team requirement)")
    return {
        "seed": seed, "errors": errors, "warnings": warnings,
        "n_pilots": pilots, "n_final_campaigns": len(campaigns) if isinstance(campaigns, list) else None,
        "metrics": {key: result[key] for key in ("net_arpu_gain", "total_cost", "total_contacts", "status")} if result else None,
        "log": logs.getvalue(),
    }


def check_submission(module_name):
    from make_submission import build_submission
    agent = importlib.import_module(module_name).Agent()
    frame = build_submission(agent, seed=42)
    csv_bytes = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return {"sha256": hashlib.sha256(csv_bytes).hexdigest(), "rows": len(frame)}


def check_demo():
    """Instrument one real call, comparing metrics with the same scoring call."""
    import agent
    import scoring_core
    original_act = agent.Agent.act
    original_score = scoring_core.score_campaigns
    calls, scored = [], []
    attempts = []

    def record_act(self, env):
        attempts.append(env)
        campaigns = original_act(self, env)
        calls.append((env, campaigns))
        return campaigns

    def record_score(*args, **kwargs):
        result = original_score(*args, **kwargs)
        scored.append(result)
        return result

    # Import within patches so both `import module` and `from module import`
    # see the instrumented functions. Each worker is a fresh process.
    with patch.object(agent.Agent, "act", record_act), patch.object(scoring_core, "score_campaigns", record_score):
        demo = importlib.import_module("demo_eval")
        value = demo.run_demo(seed=42)
    errors = []
    if len(attempts) != 1:
        errors.append(f"run_demo must call Agent.act once; observed {len(attempts)} calls")
    if not scored:
        errors.append("No call to supplied scoring_core.score_campaigns observed")
    if not isinstance(value, dict):
        return {"errors": errors + ["run_demo must return dict"]}
    for field in ("seed", "campaigns", "n_pilots", "metrics"):
        if field not in value:
            errors.append(f"Missing result field: {field}")
    if type(value.get("seed")) is not int or value["seed"] != 42:
        errors.append("Returned seed differs from requested seed")
    if not isinstance(value.get("campaigns"), list):
        errors.append("campaigns must be a list")
    if type(value.get("n_pilots")) is not int:
        errors.append("n_pilots must be int")
    if len(calls) == 1:
        env, campaigns = calls[0]
        if value.get("campaigns") != campaigns:
            errors.append("Returned campaigns differ from this Agent.act result")
        if value.get("n_pilots") != len(env.pilot_history):
            errors.append("n_pilots differs from the same run's pilot history")
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics must be dict")
    else:
        for key in ("net_arpu_gain", "total_cost", "total_contacts", "status"):
            if key not in metrics:
                errors.append(f"Missing metric: {key}")
            elif scored and metrics[key] != scored[-1][key]:
                errors.append(f"Metric {key} differs from supplied scoring_core result")
    if len(calls) == 1 and scored:
        if scored[-1]["n_campaigns"] != len(calls[0][1]) + len(calls[0][0].pilot_history):
            errors.append("Scored campaign count differs from pilots + finals")
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        errors.append(f"Result is not strict JSON for SQLite/Telegram: {exc}")
    return {"errors": errors, "act_calls": len(attempts), "score_calls": len(scored)}


def worker(args):
    os.chdir(args.project)
    sys.path.insert(0, str(args.project))
    started = time.monotonic()
    logs = io.StringIO()
    try:
        with contextlib.redirect_stdout(logs):
            if args.worker == "agent":
                result = check_agent(args.agent_module, args.seed)
            elif args.worker == "submission":
                result = check_submission(args.agent_module)
            else:
                result = check_demo()
    except Exception as exc:
        result = {"errors": [f"{type(exc).__name__}: {exc}"]}
    result["seconds"] = round(time.monotonic() - started, 3)
    if logs.getvalue():
        result["worker_log"] = logs.getvalue()
    args.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def invoke(args, kind, seed=42):
    with tempfile.TemporaryDirectory(prefix="beeline_qa_") as temp:
        result_file = Path(temp) / "result.json"
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--project", str(args.project),
                   "--agent-module", args.agent_module, "--worker", kind,
                   "--seed", str(seed), "--result-file", str(result_file)]
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       env=environment, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return {"seed": seed, "errors": [f"Timeout after {args.timeout}s"]}
        if completed.returncode or not result_file.exists():
            return {"seed": seed, "errors": [f"Worker exit {completed.returncode}"], "stderr": completed.stderr[-4000:]}
        return json.loads(result_file.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--agent-module", default="agent")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--report", type=Path, default=Path("qa_report.json"))
    parser.add_argument("--worker", choices=["agent", "submission", "demo"], help=argparse.SUPPRESS)
    parser.add_argument("--seed", type=int, default=42, help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.project = args.project.resolve()
    if not args.project.is_dir() or args.runs < 1 or args.timeout < 1:
        parser.error("Project must exist; runs and timeout must be positive")
    if args.worker:
        worker(args)
        return 0
    report = {"project": str(args.project), "agent_module": args.agent_module,
              "scope": "mock QA only; bot/SQLite adapters not yet implemented", "runs": []}
    for seed in dict.fromkeys([42, *range(args.runs)]):
        result = invoke(args, "agent", seed)
        report["runs"].append(result)
        print(f"seed={seed}: errors={len(result.get('errors', []))}, metrics={result.get('metrics')}", flush=True)
    first, second = invoke(args, "submission"), invoke(args, "submission")
    report["submission"] = {"first": first, "second": second,
                            "reproducible": bool(first.get("sha256")) and first.get("sha256") == second.get("sha256")}
    if (args.project / "demo_eval.py").is_file() and (args.project / "agent.py").is_file():
        report["demo"] = invoke(args, "demo")
    else:
        report["demo"] = {"status": "NOT_TESTED", "reason": "agent.py or demo_eval.py is missing"}
    report["bot"] = {"status": "NOT_TESTED", "reason": "Requires project-specific handler/SQLite tests against the actual bot"}
    gains = [r["metrics"]["net_arpu_gain"] for r in report["runs"] if r.get("metrics")]
    report["stability"] = {"positive": sum(v > 0 for v in gains), "evaluated": len(gains),
                           "changes_sign": any(v > 0 for v in gains) and any(v <= 0 for v in gains)}
    failed = any(r.get("errors") for r in report["runs"]) or not report["submission"]["reproducible"] or bool(report["demo"].get("errors"))
    report["status"] = "FAIL" if failed else "PARTIAL"  # Bot QA is deliberately pending.
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"QA: {report['status']}; report: {args.report.resolve()}", flush=True)
    return 1 if failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
