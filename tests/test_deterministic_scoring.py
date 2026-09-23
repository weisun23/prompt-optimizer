# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import evaluation_mode
import report_gen
import runner
import scorer


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


class EvaluationModeTest(unittest.TestCase):
    def test_auto_uses_program_scoring_for_fixed_result_task_types(self):
        for task_type in ("classification", "slot-filling", "fixed-json", "extraction"):
            with self.subTest(task_type=task_type):
                self.assertEqual("expected", evaluation_mode.resolve_eval_mode({
                    "evalMode": "auto", "promptTaskType": task_type,
                }))
        self.assertEqual("expected-semantic", evaluation_mode.resolve_eval_mode({
            "evalMode": "auto", "taskType": "inject", "promptTaskType": "summarization",
        }))
        self.assertEqual("judge", evaluation_mode.resolve_eval_mode({
            "evalMode": "auto", "taskType": "chat", "promptTaskType": "conversation",
        }))
        requirements = evaluation_mode.describe_evaluation({
            "evalMode": "auto", "promptTaskType": "slot-filling",
        })
        self.assertFalse(requirements["scoringJudgeRequired"])
        self.assertEqual(0, requirements["judgeTimesPerCase"])

    def test_runner_estimate_has_zero_judge_calls_for_deterministic_scoring(self):
        estimate = runner.estimate(
            [{"id": "c1", "type": "inject"}, {"id": "c2", "type": "inject"}],
            {"taskType": "inject", "promptTaskType": "classification", "evalMode": "auto"},
            judge_times=3,
        )
        self.assertEqual(0, estimate["judgeCalls"])
        binary = runner.estimate(
            [{"id": "c1", "type": "inject"}, {"id": "c2", "type": "inject"}],
            {"taskType": "inject", "promptTaskType": "tool-agent", "evalMode": "tool-binary"},
            judge_times=3,
        )
        self.assertFalse(evaluation_mode.uses_judge("tool-binary", {}))
        self.assertEqual(0, binary["judgeCalls"])
        semantic_binary = runner.estimate(
            [{"id": "c1", "type": "inject"}, {"id": "c2", "type": "inject"}],
            {"taskType": "inject", "promptTaskType": "tool-agent", "evalMode": "tool-binary",
             "semanticToolArgFields": ["query.note"]}, judge_times=3)
        self.assertEqual(2, semantic_binary["judgeCalls"])

    def test_report_rows_keep_comparison_method_and_judge_calls(self):
        rows = report_gen.score_rows({
            "baseline": {"case-1": {
                "caseId": "case-1", "totalScore": 100, "verdict": "PASS",
                "scoreMode": "expected",
            }}
        }, [{"id": "baseline", "kind": "full", "scoreMode": "expected",
             "scoring": {"comparisonMethod": "deterministic", "judgeCalls": 0}}])
        self.assertEqual("expected", rows[0]["scoreMode"])
        self.assertEqual("deterministic", rows[0]["comparisonMethod"])
        self.assertEqual(0, rows[0]["judgeCalls"])


class DeterministicScoringTest(unittest.TestCase):
    def score(self, task, expected, actual):
        case = {"id": "case-1", "expectedOutput": expected}
        traj = {"caseId": "case-1", "output": actual, "endReason": "completed"}
        with mock.patch.object(scorer.llm, "chat", side_effect=AssertionError("不应调用 Judge")):
            return scorer.score_case(
                {}, task, case, traj, "", ["result"], [1.0], "expected", 3, 10)

    def test_classification_label_is_compared_by_program_once(self):
        result = self.score({"promptTaskType": "classification"}, "approved", "  approved\n")
        self.assertEqual(100, result["totalScore"])
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(0, result["judgeTimes"])
        self.assertFalse(result["judgeCalled"])
        self.assertEqual("deterministic", result["comparisonMethod"])

    def test_json_comparison_ignores_object_key_order_but_keeps_types_strict(self):
        passed = self.score(
            {"promptTaskType": "slot-filling"},
            {"name": "张三", "count": 1}, '{"count": 1, "name": "张三"}')
        failed = self.score(
            {"promptTaskType": "slot-filling"},
            {"name": "张三", "count": 1}, '{"count": 1.0, "name": "张三"}')
        self.assertEqual("PASS", passed["verdict"])
        self.assertEqual(100, passed["totalScore"])
        self.assertEqual("FAIL", failed["verdict"])
        self.assertEqual(0, failed["totalScore"])
        self.assertIn("类型", "\n".join(failed["differences"]))

    def test_json_component_metrics_explain_partial_failure_without_changing_verdict(self):
        task = {"promptTaskType": "classification", "deterministicScoring": {
            "format": "json", "metricPaths": {"startLabels": "$.turns", "endLabel": "$.endLabel"}}}
        expected = {"turns": [{"turn": 2, "labels": ["咨询"]}], "endLabel": "成功"}
        actual = '{"turns":[{"turn":2,"labels":["咨询"]}],"endLabel":"失败"}'
        result = self.score(task, expected, actual)
        self.assertEqual("FAIL", result["verdict"])
        self.assertEqual({"startLabels": 100, "endLabel": 0}, result["componentScores"])

    def test_invalid_or_fenced_json_fails_strict_format_without_judge(self):
        invalid = self.score({"promptTaskType": "fixed-json"}, {"label": "A"}, "not-json")
        fenced = self.score(
            {"promptTaskType": "fixed-json"}, {"label": "A"},
            '```json\n{"label":"A"}\n```')
        self.assertEqual("format_error", invalid["failLabel"])
        self.assertEqual("format_error", fenced["failLabel"])
        self.assertEqual(0, invalid["judgeTimes"])

    def test_duplicate_keys_and_nonfinite_numbers_are_not_valid_json_results(self):
        duplicate = self.score(
            {"promptTaskType": "fixed-json"}, {"label": "A"},
            '{"label":"A","label":"A"}')
        nonfinite = self.score(
            {"promptTaskType": "fixed-json"}, {"score": 1},
            '{"score":NaN}')
        self.assertEqual("format_error", duplicate["failLabel"])
        self.assertEqual("format_error", nonfinite["failLabel"])

    def test_missing_expected_output_is_invalid_case_contract(self):
        result = scorer.score_case(
            {}, {"promptTaskType": "classification"}, {"id": "case-1"},
            {"caseId": "case-1", "output": "A", "endReason": "completed"},
            "", ["result"], [1.0], "expected", 2, 10)
        self.assertEqual("INVALID", result["verdict"])
        self.assertIsNone(result["totalScore"])
        self.assertEqual("case_error", result["failLabel"])

    def test_execution_error_is_invalid_instead_of_model_failure(self):
        case = {"id": "case-1", "expectedOutput": "A"}
        traj = {"caseId": "case-1", "error": "candidate timeout", "messages": []}
        with mock.patch.object(scorer.llm, "chat", side_effect=AssertionError("不应调用 Judge")):
            result = scorer.score_case(
                {}, {"promptTaskType": "classification"}, case, traj,
                "", ["result"], [1.0], "expected", 2, 10)
        self.assertEqual("INVALID", result["verdict"])
        self.assertIsNone(result["totalScore"])
        self.assertEqual("device_error", result["failLabel"])

    def test_semantic_expected_mode_remains_an_explicit_judge_option(self):
        task = {"promptTaskType": "summarization", "description": "总结",
                "judgePassThreshold": 90, "scoreDisagreementThreshold": 10}
        case = {"id": "case-1", "expectedOutput": "核心结论"}
        traj = {"caseId": "case-1", "output": "结论的同义表达", "endReason": "completed"}
        reply = {"content": '{"score":100,"reason":"语义一致","differences":[],"failLabel":"none"}'}
        with mock.patch.object(scorer.llm, "chat", return_value=reply) as chat:
            result = scorer.score_case(
                {"models": {"judge": {"model": "judge"}}}, task, case, traj,
                "", ["result"], [1.0], "expected-semantic", 1, 10)
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(1, result["judgeTimes"])
        chat.assert_called_once()
        rendered = chat.call_args.args[2][0]["content"]
        self.assertIn("score低于90", rendered)
        self.assertNotIn("score低于60", rendered)

    def test_semantic_judge_requires_task_confirmed_thresholds(self):
        task = {"promptTaskType": "summarization", "description": "总结"}
        case = {"id": "case-1", "expectedOutput": "核心结论"}
        traj = {"caseId": "case-1", "output": "表达", "endReason": "completed"}
        with self.assertRaisesRegex(ValueError, "judgePassThreshold"):
            scorer.score_case({}, task, case, traj, "", ["result"], [1.0],
                              "expected-semantic", 1, 10)

    def test_semantic_expected_without_expected_output_is_invalid_without_judge(self):
        with mock.patch.object(scorer.llm, "chat", side_effect=AssertionError("不应调用 Judge")):
            result = scorer.score_case(
                {}, {"promptTaskType": "summarization"}, {"id": "case-1"},
                {"caseId": "case-1", "output": "summary", "endReason": "completed"},
                "", ["result"], [1.0], "expected-semantic", 1, 10)
        self.assertEqual("INVALID", result["verdict"])
        self.assertEqual("case_error", result["failLabel"])

    def test_cli_scores_classification_without_judge_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            trajectories = os.path.join(root, "trajectories")
            scores = os.path.join(root, "scores", "baseline")
            config_path = os.path.join(root, "config.json")
            task_path = os.path.join(root, "task.json")
            cases_path = os.path.join(root, "cases.json")
            os.makedirs(trajectories)
            write_json(config_path, {"models": {"candidate": {"model": "candidate"}}})
            write_json(task_path, {"taskType": "inject", "promptTaskType": "classification",
                       "evalMode": "auto"})
            write_json(cases_path, [{"id": "case-1", "expectedOutput": "A"}])
            write_json(os.path.join(trajectories, "case-1.json"), {
                "caseId": "case-1", "output": "A", "endReason": "completed"})
            argv = ["scorer.py", "--config", config_path, "--task", task_path,
                    "--trajectories", trajectories, "--cases", cases_path, "--out", scores]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(scorer.llm, "chat", side_effect=AssertionError("不应调用 Judge")):
                scorer.main()
            with open(os.path.join(scores, "case-1.json"), encoding="utf-8") as stream:
                result = json.load(stream)
            with open(os.path.join(scores, "_summary.json"), encoding="utf-8") as stream:
                summary = json.load(stream)
            with open(os.path.join(root, "run_manifest.json"), encoding="utf-8") as stream:
                invocation = json.load(stream)["invocations"][0]
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(0, result["judgeTimes"])
        self.assertEqual(0, summary["judgeCalls"])
        self.assertEqual("deterministic", summary["comparisonMethod"])
        self.assertEqual("deterministic", invocation["scoring"]["comparisonMethod"])
        self.assertEqual(0, invocation["scoring"]["judgeCalls"])


if __name__ == "__main__":
    unittest.main()
