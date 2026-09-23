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

import score_protocol


class ScoreProtocolTest(unittest.TestCase):
    def test_normalizes_legacy_binary_case(self):
        row = score_protocol.normalize_score({
            "caseId": "case-1",
            "score": 100,
            "reason": "正确",
            "errors": [],
        })
        self.assertEqual(100, row["totalScore"])
        self.assertEqual("PASS", row["verdict"])
        self.assertEqual("tool-binary", row["scoreMode"])

    def test_normalizes_zero_binary_case_as_failure(self):
        row = score_protocol.normalize_score({
            "caseId": "case-2",
            "score": 0,
            "reason": "函数错误",
            "errors": ["函数错误"],
        })
        self.assertEqual(0, row["totalScore"])
        self.assertEqual("FAIL", row["verdict"])
        self.assertEqual("tool_misuse", row["failLabel"])

    def test_preserves_invalid_without_scoring_it(self):
        row = score_protocol.normalize_score({
            "caseId": "case-3",
            "totalScore": None,
            "verdict": "INVALID",
            "failLabel": "device_error",
        })
        self.assertIsNone(row["totalScore"])
        self.assertEqual("INVALID", row["verdict"])

    def test_expected_eval_mode_keeps_expected_score_mode(self):
        row = score_protocol.normalize_score({
            "caseId": "expected-1", "evalMode": "expected",
            "totalScore": 80, "verdict": "PASS",
        })
        self.assertEqual("expected", row["scoreMode"])

    def test_semantic_expected_mode_stays_distinct_from_program_expected(self):
        row = score_protocol.normalize_score({
            "caseId": "expected-semantic-1", "evalMode": "expected-semantic",
            "totalScore": 80, "verdict": "PASS",
        })
        self.assertEqual("expected-semantic", row["scoreMode"])

    def test_rubric_score_without_recorded_verdict_is_invalid_not_guessed_at_sixty(self):
        row = score_protocol.normalize_score({"caseId": "legacy-rubric", "totalScore": 80})
        self.assertEqual("INVALID", row["verdict"])
        self.assertEqual("score_contract_error", row["failLabel"])

    def test_load_round_excludes_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "case-1.json"), "w", encoding="utf-8") as f:
                json.dump({"caseId": "case-1", "score": 100}, f)
            with open(os.path.join(tmp, "_summary.json"), "w", encoding="utf-8") as f:
                json.dump({"count": 1, "score": 100}, f)

            rows, summary = score_protocol.load_score_round(tmp)

        self.assertEqual(["case-1"], sorted(rows))
        self.assertEqual(1, summary["count"])

    def test_summary_uses_only_valid_scores(self):
        summary = score_protocol.summarize_scores({
            "a": {"caseId": "a", "score": 100},
            "b": {"caseId": "b", "score": 0},
            "c": {"caseId": "c", "verdict": "INVALID", "totalScore": None},
        })
        self.assertEqual(3, summary["count"])
        self.assertEqual(2, summary["scoredCount"])
        self.assertEqual(1, summary["invalidCount"])
        self.assertEqual(50.0, summary["avg"])
        self.assertEqual(50.0, summary["passRate"])

    def test_make_binary_score_writes_compatible_fields(self):
        row = score_protocol.make_binary_score("case-1", 0, "错误", ["错误"], judge_called=False)
        self.assertEqual(0, row["score"])
        self.assertEqual(0, row["totalScore"])
        self.assertEqual("FAIL", row["verdict"])
        self.assertEqual("tool-binary", row["scoreMode"])
        self.assertFalse(row["judgeCalled"])

    def test_binary_scorer_exposes_the_same_contract(self):
        import score_tool_binary

        row = score_tool_binary.binary_result(
            "case-4", 100, "正确", [], False, toolCheck="一致"
        )
        self.assertEqual("tool-binary", row["scoreMode"])
        self.assertEqual(100, row["totalScore"])
        self.assertEqual("PASS", row["verdict"])

    def test_binary_scorer_marks_device_error_invalid_without_calling_judge(self):
        import score_tool_binary

        original_chat = score_tool_binary.llm.chat
        score_tool_binary.llm.chat = lambda *args, **kwargs: self.fail("装置错误不应调用 Judge")
        try:
            row = score_tool_binary.score_one(
                {}, {}, {"id": "case-invalid", "toolsExpect": []},
                {
                    "caseId": "case-invalid",
                    "endReason": "device_error",
                    "deviceErrors": [{"type": "mock_exhausted", "message": "Mock 已耗尽"}],
                    "messages": [],
                },
                1,
            )
        finally:
            score_tool_binary.llm.chat = original_chat

        self.assertEqual("INVALID", row["verdict"])
        self.assertIsNone(row["totalScore"])
        self.assertIsNone(row["score"])
        self.assertEqual("device_error", row["failLabel"])
        self.assertFalse(row["judgeCalled"])

    def test_exact_tool_contract_is_scored_without_judge(self):
        import score_tool_binary

        case = {
            "id": "case-exact", "toolsExpect": ["query"],
            "expectedToolArgs": {"query": {"time": "18:00"}},
            "expectedCallPolicy": {"mode": "immediate", "allowedClarifications": 0,
                                   "maxAdditionalTurns": 0},
        }
        task = {"tools": [{"type": "function", "function": {"name": "query",
                 "parameters": {"type": "object", "properties": {"time": {"type": "string"}}}}}]}
        trajectory = {"caseId": "case-exact", "endReason": "completed",
                      "toolCalls": [{"name": "query", "arguments": {"time": "18:00"},
                                     "assistantTurn": 1}], "messages": []}
        original_chat = score_tool_binary.llm.chat
        score_tool_binary.llm.chat = lambda *args, **kwargs: self.fail("精确契约不应调用 Judge")
        try:
            row = score_tool_binary.score_one({}, task, case, trajectory, 10)
        finally:
            score_tool_binary.llm.chat = original_chat
        self.assertEqual("PASS", row["verdict"])
        self.assertFalse(row["judgeCalled"])

    def test_immediate_tool_policy_rejects_delayed_call(self):
        import score_tool_binary

        case = {"id": "case-delayed", "toolsExpect": ["query"],
                "expectedToolArgs": {"query": {}},
                "expectedCallPolicy": {"mode": "immediate", "allowedClarifications": 0,
                                       "maxAdditionalTurns": 0}}
        trajectory = {"caseId": "case-delayed", "endReason": "completed",
                      "toolCalls": [{"name": "query", "arguments": {}, "assistantTurn": 2}],
                      "messages": []}
        row = score_tool_binary.score_one({}, {}, case, trajectory, 10)
        self.assertEqual("FAIL", row["verdict"])
        self.assertIn("immediate", row["reason"])

    def test_declared_semantic_extra_tool_field_is_judged_instead_of_mechanically_rejected(self):
        import score_tool_binary

        case = {"id": "semantic-extra", "toolsExpect": ["update"],
                "expectedToolArgs": {"update": {"time": "20:00"}},
                "expectedCallPolicy": {"mode": "immediate", "allowedClarifications": 0,
                                       "maxAdditionalTurns": 0}}
        task = {"semanticToolArgFields": ["date"], "tools": [
            {"type": "function", "function": {"name": "update", "parameters": {
                "type": "object", "properties": {"time": {"type": "string"},
                                                    "date": {"type": "string"}}}}}]}
        trajectory = {"caseId": "semantic-extra", "endReason": "completed",
                      "toolCalls": [{"name": "update", "arguments": {
                          "time": "20:00", "date": "2026-09-18"}, "assistantTurn": 1}],
                      "messages": [{"role": "user", "content": "改到今晚8点"}]}
        reply = {"content": '{"score":100,"reason":"日期有上下文依据","errors":[]}'}
        with mock.patch.object(score_tool_binary.llm, "chat", return_value=reply) as chat:
            row = score_tool_binary.score_one({}, task, case, trajectory, 10)
        self.assertEqual("PASS", row["verdict"])
        self.assertTrue(row["judgeCalled"])
        chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
