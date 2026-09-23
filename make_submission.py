"""Generate root submission.csv using the canonical agent and seed-42 exporter."""

from scripts.agent import Agent
from scripts.config import ROOT
from scripts.local_eval import build_submission


def main():
    frame = build_submission(Agent(), seed=42)
    path = ROOT / "submission.csv"
    frame.to_csv(path, index=False)
    print(f"Final campaigns: {len(frame)}")
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
