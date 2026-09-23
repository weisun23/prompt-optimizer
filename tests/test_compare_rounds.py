# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import compare_rounds


def write_score(root, round_name, case_id, score):
    path = os.path.join(root, "scores", round_name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, case_id + ".json"), "w", encoding="utf-8") as f:
        json.dump({"caseId": case_id, "score": score}, f)


class CompareRoundsTest(unittest.TestCase):
    def test_binary_stats_are_not_reported_as_zero_and_all_pass(self):
        scores = {
            "a": {"caseId": "a", "score": 100},
            "b": {"caseId": "b", "score": 0},
        }
        result = compare_rounds.stats(scores)
        self.assertEqual(50.0, result["avg"])
        self.assertEqual(50.0, result["passRate"])
        self.assertEqual(["b"], result["fails"])

    def test_invalid_cases_are_not_counted(self):
        result = compare_rounds.stats({
            "a": {"caseId": "a", "score": 100},
            "b": {"caseId": "b", "verdict": "INVALID", "totalScore": None},
        })
        self.assertEqual(1, result["count"])
        self.assertEqual(1, result["invalidCount"])
        self.assertEqual(100.0, result["passRate"])

    def test_invalid_only_difference_does_not_break_case_set_guard(self):
        base = {
            "a": {"caseId": "a", "score": 100},
            "device": {"caseId": "device", "verdict": "INVALID", "totalScore": None},
        }
        current = {"a": {"caseId": "a", "score": 100}}
        left, right, scope = compare_rounds.align_case_sets(base, current, allow_subset=False)
        self.assertEqual(["a"], sorted(left))
        self.assertEqual(["a"], sorted(right))
        self.assertFalse(scope["isSubset"])

    def test_different_case_sets_are_rejected_by_default(self):
        base = {"a": {"score": 100}, "b": {"score": 0}}
        current = {"b": {"score": 100}}
        with self.assertRaisesRegex(ValueError, "用例集合不一致"):
            compare_rounds.align_case_sets(base, current, allow_subset=False)

    def test_subset_comparison_uses_only_intersection_and_labels_scope(self):
        base = {"a": {"score": 100}, "b": {"score": 0}}
        current = {"b": {"score": 100}}
        left, right, scope = compare_rounds.align_case_sets(base, current, allow_subset=True)
        self.assertEqual(["b"], sorted(left))
        self.assertEqual(["b"], sorted(right))
        self.assertTrue(scope["isSubset"])
        self.assertEqual(2, scope["baseCount"])
        self.assertEqual(1, scope["currentCount"])
        self.assertEqual(1, scope["intersectionCount"])

    def test_cli_rejects_full_vs_regression_without_explicit_subset(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_score(tmp, "full", "a", 100)
            write_score(tmp, "full", "b", 0)
            write_score(tmp, "regression", "b", 100)
            command = [sys.executable, os.path.join(BIN, "compare_rounds.py"),
                       "--run", tmp, "--rounds", "full,regression"]
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", env=env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("用例集合不一致", result.stdout + result.stderr)

    def test_recorded_decisions_preserve_execution_order_past_round_9(self):
        with tempfile.TemporaryDirectory() as tmp:
            for round_name in ("round1", "round2", "round10"):
                write_score(tmp, round_name, "a", 100)
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            script = os.path.join(BIN, "compare_rounds.py")
            commands = [
                [sys.executable, script, "--run", tmp, "--rounds", "round1,round2",
                 "--record", "--version", "prompt_v2.md", "--decision", "accepted"],
                [sys.executable, script, "--run", tmp, "--rounds", "round2,round10",
                 "--record", "--version", "prompt_v10.md", "--decision", "accepted"],
            ]
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", env=env)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            with open(os.path.join(tmp, "rounds.json"), encoding="utf-8") as f:
                log = json.load(f)
        self.assertEqual(["round2", "round10"], [row["round"] for row in log])

    def test_cli_only_outputs_evidence_without_universal_acceptance_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_score(tmp, "baseline", "a", 0)
            write_score(tmp, "candidate", "a", 100)
            command = [sys.executable, os.path.join(BIN, "compare_rounds.py"),
                       "--run", tmp, "--rounds", "baseline,candidate"]
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("不自动接受或回退", result.stdout)
        self.assertNotIn("阈值 80%", result.stdout)

    def test_recording_same_round_appends_evidence_instead_of_erasing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_score(tmp, "base", "a", 0)
            write_score(tmp, "candidate", "a", 100)
            script = os.path.join(BIN, "compare_rounds.py")
            command = [sys.executable, script, "--run", tmp,
                       "--rounds", "base,candidate", "--record",
                       "--version", "prompt_v2.md", "--decision", "accepted"]
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            for _ in range(2):
                result = subprocess.run(command, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", env=env)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            with open(os.path.join(tmp, "rounds.json"), encoding="utf-8") as stream:
                rows = json.load(stream)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["eventType"] == "comparison-recorded" for row in rows))


if __name__ == "__main__":
    unittest.main()
