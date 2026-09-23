"""Compatibility entry point: python local_eval.py [--runs 10]."""

from scripts.local_eval import evaluate_agent, main, run_demo

__all__ = ["evaluate_agent", "run_demo"]

if __name__ == "__main__":
    main()
