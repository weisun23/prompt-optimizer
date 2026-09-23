# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import report_gen


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


class ReportGeneratorTest(unittest.TestCase):
    def test_report_is_blocked_for_rejected_candidate_without_user_request(self):
        self.assertTrue(report_gen.report_gate_errors("CANDIDATE_REJECTED_CONTINUE", False))
        self.assertEqual([], report_gen.report_gate_errors("ACCEPTED", False))
        self.assertEqual([], report_gen.report_gate_errors("IN_PROGRESS", True))

    def make_run(self, root):
        run = os.path.join(root, "tasks", "demo", "runs", "run-1")
        os.makedirs(run, exist_ok=True)
        for version in (1, 4, 6):
            with open(os.path.join(run, "prompt_v%d.md" % version), "w", encoding="utf-8") as f:
                f.write("prompt-%d" % version)
        return run

    def test_binary_scores_are_normalized_and_summary_is_not_a_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            score_dir = os.path.join(run, "scores", "baseline")
            write_json(os.path.join(score_dir, "a.json"), {"caseId": "a", "score": 100})
            write_json(os.path.join(score_dir, "b.json"), {"caseId": "b", "score": 0})
            write_json(os.path.join(score_dir, "_summary.json"), {
                "count": 2, "correctCount": 1, "score": 50.0,
            })

            rounds = report_gen.find_scores(run, ["baseline"])
            rows = report_gen.score_rows(rounds)

        self.assertEqual(["a", "b"], sorted(rounds["baseline"]))
        self.assertEqual(50.0, rows[0]["avg"])
        self.assertEqual(2, rows[0]["count"])
        self.assertEqual(1, rows[0]["pass"])

    def test_default_output_is_under_workspace_reports_not_tasks_reports(self):
        run = os.path.join("D:\\workspace", "tasks", "demo", "runs", "run-1")
        expected = os.path.join("D:\\workspace", "reports", "demo", "run-1", "report.html")
        self.assertEqual(os.path.normpath(expected), report_gen.default_output_path(run))

    def test_multiple_rounds_require_manifest_or_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "scores", "a", "x.json"), {"caseId": "x", "score": 100})
            write_json(os.path.join(run, "scores", "b", "x.json"), {"caseId": "x", "score": 0})

            with self.assertRaisesRegex(ValueError, "多个分数目录"):
                report_gen.resolve_report_selection(run)

    def test_manifest_without_report_markers_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "run_manifest.json"), {
                "schemaVersion": 1,
                "invocations": [
                    {"id": "full", "kind": "full", "scoreDir": "scores/full", "trajectoryDir": "trajectories-full"},
                    {"id": "regress", "kind": "regression", "scoreDir": "scores/regress", "trajectoryDir": "trajectories-regress"},
                ],
            })
            with self.assertRaisesRegex(ValueError, "includeInReport"):
                report_gen.resolve_report_selection(run)

    def test_named_trajectory_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "trajectories-baseline", "a.json"), {
                "caseId": "a", "messages": [], "endReason": "completed",
            })
            trajs = report_gen.find_trajectories(run, "trajectories-baseline")
        self.assertEqual(["a"], sorted(trajs))

    def test_zero_or_mismatched_trajectories_fail_closed(self):
        scores = {"a": {"caseId": "a", "score": 100}}
        with self.assertRaisesRegex(ValueError, "轨迹数量为 0"):
            report_gen.validate_trajectory_coverage({}, {})
        with self.assertRaisesRegex(ValueError, "轨迹数量为 0"):
            report_gen.validate_trajectory_coverage(scores, {})
        with self.assertRaisesRegex(ValueError, "用例集合不一致"):
            report_gen.validate_trajectory_coverage(scores, {
                "b": {"caseId": "b", "messages": []},
            })

    def test_manifest_final_prompt_beats_highest_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            manifest = {
                "schemaVersion": 1,
                "finalPrompt": "prompt_v4.md",
                "rounds": [],
            }
            prompts, _ = report_gen.find_prompts(run)
            chosen = report_gen.choose_final_prompt(run, prompts, manifest)
        self.assertEqual("prompt_v4.md", chosen[1])

    def test_all_rolled_back_decisions_leave_final_prompt_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "rounds.json"), [
                {"version": "prompt_v4.md", "decision": "rolled_back"},
                {"version": "prompt_v6.md", "decision": "rolled_back"},
            ])
            prompts, _ = report_gen.find_prompts(run)
            chosen = report_gen.choose_final_prompt(run, prompts)
        self.assertIsNone(chosen)

    def test_report_decision_table_hides_started_events_but_keeps_completed_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "rounds.json"), [
                {"eventType": "attempt-started", "attemptId": "attempt-001",
                 "version": "prompt_v4.md", "changeSummary": "准备验证边界"},
                {"eventType": "attempt-completed", "attemptId": "attempt-001",
                 "round": "candidate-v4", "version": "prompt_v4.md",
                 "decision": "rolled_back", "reason": "保护集退化"},
            ])
            html = report_gen.rounds_section(run)
        self.assertNotIn("准备验证边界", html)
        self.assertIn("candidate-v4", html)
        self.assertIn("保护集退化", html)

    def test_html_is_responsive_filterable_and_preserves_safe_line_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.make_run(tmp)
            write_json(os.path.join(run, "scores", "baseline", "a.json"), {
                "caseId": "a", "score": 0, "reason": "错误",
                "taxonomyValues": {"business_stage": "executing", "impact": "p0"},
            })
            write_json(os.path.join(run, "scores", "baseline", "_summary.json"), {
                "count": 1, "correctCount": 0, "score": 0,
            })
            write_json(os.path.join(run, "trajectories-baseline", "a.json"), {
                "caseId": "a", "endReason": "completed",
                "messages": [{
                    "role": "assistant", "content": "调用工具",
                    "tool_calls": [
                        {"name": "first", "arguments": {"x": 1}},
                        {"name": "second", "arguments": {"y": 2}},
                    ],
                }],
            })
            write_json(os.path.join(run, "run_manifest.json"), {
                "schemaVersion": 1,
                "finalPrompt": "prompt_v4.md",
                "rounds": [{
                    "id": "baseline", "kind": "full",
                    "scoreDir": "scores/baseline",
                    "trajectoryDir": "trajectories-baseline",
                    "prompt": "prompt_v4.md",
                }],
            })
            out = os.path.join(tmp, "report.html")
            report_gen.build(run, out, "", report_gen.resolve_report_selection(run))
            with open(out, encoding="utf-8") as f:
                html = f.read()

        self.assertIn("@media", html)
        self.assertNotIn("minmax(480px", html)
        self.assertIn('id="case-filter"', html)
        self.assertIn('id="verdict-filter"', html)
        self.assertIn('id="tool-filter"', html)
        self.assertIn('id="taxonomy-filter"', html)
        self.assertIn("business_stage", html)
        self.assertIn("executing", html)
        self.assertIn("每页", html)
        self.assertIn("first", html)
        self.assertIn("second", html)
        self.assertNotIn("&lt;br&gt;", html)
        self.assertNotIn("&lt;b&gt;本结论", html)
        self.assertIn("prompt_v4.md", html)


if __name__ == "__main__":
    unittest.main()
