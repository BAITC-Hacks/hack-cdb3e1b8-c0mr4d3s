"""The judge-facing Agent import must work without importing the bot or database."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SubmissionEntrypointTests(unittest.TestCase):
    def test_root_agent_runs_from_another_directory_without_bot_or_database(self):
        project = Path(__file__).resolve().parents[1]
        program = """
import sys
sys.path.insert(0, sys.argv[1])
from agent import Agent
from scripts.agent import Agent as Implementation
from scripts.config import AgentConfig
assert Agent is Implementation
assert 'scripts.bot' not in sys.modules
assert 'telegram' not in sys.modules
assert 'sqlite3' not in sys.modules
from scripts.environment import make_mock_env
env, _ = make_mock_env(seed=42)
campaigns = Agent(settings=AgentConfig()).act(env)
assert 1 <= len(campaigns) <= 10
assert 1 <= len(env.pilot_history) <= 20
assert 'scripts.bot' not in sys.modules
assert 'telegram' not in sys.modules
assert 'sqlite3' not in sys.modules
"""
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-B", "-c", program, str(project)],
                cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=60,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
