"""Regression tests for multi-seed evaluation error reporting."""

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from scripts import local_eval


class LocalEvalTests(unittest.TestCase):
    def test_runs_reports_crash_and_includes_completed_pilots(self):
        def crash_after_pilot(env):
            env.run_pilot(target_tariff="tariff_8", channel="sms", n_customers=50)
            raise RuntimeError("private external details")

        failing_agent = Mock()
        failing_agent.act.side_effect = crash_after_pilot
        successful_agent = Mock()
        successful_agent.act.return_value = []
        results = []
        evaluate_agent = local_eval.evaluate_agent

        def capture_result(*args, **kwargs):
            result = evaluate_agent(*args, **kwargs)
            results.append(result)
            return result

        output = io.StringIO()
        with patch("sys.argv", ["local_eval", "--runs", "2"]), \
                patch.object(local_eval, "Agent", side_effect=[failing_agent, successful_agent]), \
                patch.object(local_eval, "evaluate_agent", side_effect=capture_result), \
                redirect_stdout(output):
            local_eval.main()

        text = output.getvalue()
        self.assertIn("[!] seed  0: Agent.act завершился ошибкой (RuntimeError)", text)
        self.assertIn("Пилотов проведено: 1; их результат включён в оценку", text)
        self.assertIn("прогонов без ошибок: 1 из 2", text)
        self.assertIn("прогонов с ошибкой: 1 из 2 (seed: 0)", text)
        self.assertIn("Метрики прогонов без ошибок:", text)
        self.assertNotIn("private external details", text)
        self.assertEqual(results[0]["n_pilots"], 1)
        self.assertEqual(results[0]["metrics"]["total_contacts"], 50)
        self.assertEqual(results[0]["metrics"]["total_cost"], 200)
        failed_net = results[0]["metrics"]["net_arpu_gain"]
        successful_net = results[1]["metrics"]["net_arpu_gain"]
        self.assertIn(f"seed  0: чистый результат {failed_net:>14,.0f}", text)
        self.assertIn(f"медиана: {successful_net:,.0f}   минимум: {successful_net:,.0f}"
                      f"   максимум: {successful_net:,.0f}", text)
        self.assertIn("прогонов в плюс: 0 из 1", text)

    def test_all_runs_crash_without_printing_empty_statistics(self):
        output = io.StringIO()
        result = {"metrics": {"net_arpu_gain": 100}, "n_pilots": 1,
                  "error": "Agent.act завершился ошибкой (RuntimeError)"}
        with patch("sys.argv", ["local_eval", "--runs", "2"]), \
                patch.object(local_eval, "Agent"), \
                patch.object(local_eval, "evaluate_agent", return_value=result), \
                redirect_stdout(output):
            local_eval.main()

        text = output.getvalue()
        self.assertIn("[!] seed  0:", text)
        self.assertIn("[!] seed  1:", text)
        self.assertIn("прогонов без ошибок: 0 из 2", text)
        self.assertIn("прогонов с ошибкой: 2 из 2 (seed: 0, 1)", text)
        self.assertIn("Нет прогонов без ошибок — статистика устойчивости недоступна", text)
        self.assertNotIn("медиана:", text)
        self.assertNotIn("прогонов в плюс:", text)
        self.assertNotIn("nan", text)

    def test_runs_without_crashes_reports_all_completed(self):
        output = io.StringIO()
        results = [{"metrics": {"net_arpu_gain": 100}},
                   {"metrics": {"net_arpu_gain": 300}}]
        with patch("sys.argv", ["local_eval", "--runs", "2"]), \
                patch.object(local_eval, "Agent"), \
                patch.object(local_eval, "evaluate_agent", side_effect=results), \
                redirect_stdout(output):
            local_eval.main()

        text = output.getvalue()
        self.assertIn("прогонов без ошибок: 2 из 2", text)
        self.assertIn("медиана: 200   минимум: 100   максимум: 300", text)
        self.assertIn("прогонов в плюс: 2 из 2", text)
        self.assertNotIn("[!]", text)
        self.assertNotIn("прогонов с ошибкой", text)


if __name__ == "__main__":
    unittest.main()
