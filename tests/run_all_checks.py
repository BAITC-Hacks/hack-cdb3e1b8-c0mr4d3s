"""Run local automated QA in a disposable project copy; no Telegram polling.

python tests/run_all_checks.py --project . --output PATH
Requires simplified scripts/ layout and Gemini disabled in the test copy.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("test_results"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir() or args.timeout < 1:
        parser.error("Existing project and positive timeout required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output = args.output.resolve() / stamp
    output.mkdir(parents=True)
    qa = Path(__file__).resolve().parent
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                       QA_PROJECT=str(project))
    environment.pop("TELEGRAM_BOT_TOKEN", None)
    report = {"created_at": datetime.now(timezone.utc).isoformat(),
              "project": str(project), "steps": [], "test_count": 0,
              "live_telegram": "NOT_TESTED", "release_status": "PARTIAL", "completed": False}
    report["local_qa_additions"] = {
        name: hashlib.sha256((qa / name).read_bytes()).hexdigest()
        for name in ("run_all_checks.py", "test_bot_storage_failures.py")
    }

    def save():
        report["automated_status"] = "FAIL" if any(s["status"] == "FAIL" for s in report["steps"]) else ("PASS" if report["completed"] else "RUNNING")
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    def fail(name, message):
        report["steps"].append({"name": name, "status": "FAIL", "error": message})
        save()
        print(f"FAIL {name}: {message}", flush=True)

    def run(name, arguments, accepted=(0,), minimum_tests=None):
        log = output / (name + ".log")
        try:
            done = subprocess.run([sys.executable, "-B", *map(str, arguments)], cwd=project,
                env=environment, capture_output=True, encoding="utf-8", errors="replace", timeout=args.timeout)
            text = done.stdout + "\n" + done.stderr
            log.write_text(text, encoding="utf-8")
            count = sum(int(n) for n in re.findall(r"Ran (\d+) tests? in", text))
            passed = done.returncode in accepted and (minimum_tests is None or count >= minimum_tests)
            step = {"name": name, "status": "PASS" if passed else "FAIL", "exit_code": done.returncode,
                    "tests": count, "log": log.name}
        except subprocess.TimeoutExpired:
            step = {"name": name, "status": "FAIL", "error": f"Exceeded {args.timeout} seconds"}
        except OSError as exc:
            step = {"name": name, "status": "FAIL", "error": type(exc).__name__}
        report["steps"].append(step)
        report["test_count"] += step.get("tests", 0)
        save()
        print(f"{step['status']} {name}" + (f" ({step['tests']} tests)" if step.get("tests") else ""), flush=True)
        return step["status"] == "PASS"

    try:
        config_path = project / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
        if config.get("gemini", {}).get("enabled", False) is not False:
            raise ValueError("Disable gemini.enabled in this test copy before offline QA")
        if "runner_module" in config.get("bot", {}):
            raise ValueError("Remove obsolete bot.runner_module from this test copy")
        if not (project / "scripts/local_eval.py").is_file():
            raise ValueError("Set --project to the simplified main with scripts/local_eval.py")
        versions = {}
        for line in (project / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            package, expected = line.strip().split("==")
            actual = importlib.metadata.version(package)
            if actual != expected:
                raise ValueError(f"Dependency mismatch: {package}")
            versions[package] = actual
        report["versions"] = versions
        provenance_path = project / "tests" / "source_manifest.json"
        if provenance_path.exists():
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            for entry in provenance["main"] + provenance.get("qa", []):
                raw = (project / entry["path"]).read_bytes()
                variants = (raw, raw.replace(b"\r\n", b"\n"))
                hashes = {hashlib.sha1(f"blob {len(value)}\0".encode() + value).hexdigest() for value in variants}
                if entry["sha"] not in hashes:
                    raise ValueError(f"Source changed since review: {entry['path']}")
            report["source_refs"] = provenance["refs"]
        report["steps"].append({"name": "preflight", "status": "PASS", "pinned_packages": len(versions)})
    except Exception as exc:
        # Config parser errors can contain fragments of a private config. Do not print them.
        message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("Disable ", "Remove ", "Set ", "Dependency mismatch", "Source changed")) else type(exc).__name__
        fail("preflight", message)
        return 1

    run("dependencies", ["-m", "pip", "check"])
    run("all_tests", ["-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"], minimum_tests=69)
    run("official_eval_once", ["local_eval.py"])
    run("official_eval_ten", ["local_eval.py", "--runs", "10"])
    strict_path = output / "strict_qa.json"
    if run("strict_qa", [qa / "qa_check.py", "--project", project, "--runs", "10", "--report", strict_path], accepted=(2,)):
        strict = json.loads(strict_path.read_text(encoding="utf-8"))
        report["stability"] = strict["stability"]
        report["demo_contract_observation"] = strict["demo"]
        if strict["demo"].get("act_calls") != 1 or strict["demo"].get("errors"):
            fail("strict_demo_observation", "The real demo contract was not verified")

    submission_path = project / "submission.csv"
    previous = submission_path.read_bytes() if submission_path.exists() else None
    exports = []
    try:
        for number in (1, 2):
            if run(f"official_export_{number}", ["make_submission.py"]):
                content = submission_path.read_bytes()
                exports.append(hashlib.sha256(content).hexdigest())
                (output / f"submission_{number}.csv").write_bytes(content)
        if len(exports) == 2 and exports[0] == exports[1]:
            report["steps"].append({"name": "export_reproducibility", "status": "PASS", "sha256": exports[0]})
        else:
            fail("export_reproducibility", "Exports failed or differed")
    finally:
        if previous is not None:
            submission_path.write_bytes(previous)

    if run("native_package_export", ["-m", "scripts.local_eval", "--submission"]):
        native_hash = hashlib.sha256((project / "data/submission.csv").read_bytes()).hexdigest()
        if not exports or native_hash != exports[0]:
            fail("export_entrypoint_equivalence", "Root and package exports differ")
        else:
            report["steps"].append({"name": "export_entrypoint_equivalence", "status": "PASS"})
    for mode in ("offline", "timeout", "invalid_json", "invalid_ids"):
        run("provider_" + mode, [qa / "check_gemini.py", "--project", project,
            "--mode", mode, "--report", output / ("provider_" + mode + ".json")])

    db_path = output / "local_runs.sqlite3"
    if run("bot_once", ["-m", "scripts.bot", "--once", "--backend", "real", "--db", db_path]):
        # A read-only new connection verifies the subprocess persisted its result.
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM bot_runs ORDER BY id DESC").fetchall()
        if len(rows) != 1 or rows[0]["state"] != "done" or rows[0]["seed"] != 42:
            fail("bot_persistence", "Expected exactly one successful seed-42 run")
        else:
            row = dict(rows[0])
            report["bot_result"] = {"seed": row["seed"], "n_pilots": row["n_pilots"],
                "campaigns": json.loads(row["campaigns_json"]), "metrics": json.loads(row["metrics_json"])}
            report["steps"].append({"name": "bot_persistence", "status": "PASS"})
    report["completed"] = True
    save()
    print(f"AUTOMATED QA: {report['automated_status']}; tests: {report['test_count']}", flush=True)
    print("Live Telegram: NOT_TESTED; release readiness: PARTIAL", flush=True)
    print(f"Report: {output / 'summary.json'}", flush=True)
    return 0 if report["automated_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
