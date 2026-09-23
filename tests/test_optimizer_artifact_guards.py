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

import baseline_guard
import capability_guard
import case_adjudication
import select_cases
import workflow_policy


class ArtifactGuardTest(unittest.TestCase):
    def test_case_selector_preserves_requested_order_and_rejects_missing_ids(self):
        cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(["c", "a"], [row["id"] for row in
                                      select_cases.select_cases(cases, ["c", "a"])])
        with self.assertRaisesRegex(ValueError, "missing"):
            select_cases.select_cases(cases, ["missing"])

    def test_production_candidate_must_preserve_required_capabilities(self):
        inventory = {"schemaVersion": 1, "items": [
            {"id": "booking", "description": "订餐流程", "required": True,
             "status": "preserved", "candidateMarkers": ["订餐"]},
            {"id": "safety", "description": "安全规则", "required": True,
             "status": "deferred-experimental", "candidateMarkers": []},
        ]}
        errors = "\n".join(capability_guard.validate_inventory(
            inventory, "production-candidate", "这里包含订餐流程"))
        self.assertIn("safety", errors)
        self.assertEqual([], capability_guard.validate_inventory(
            inventory, "experimental", "这里包含订餐流程"))

    def test_baseline_candidates_must_use_same_production_scope(self):
        selection = {"schemaVersion": 1, "selectedId": "old",
                     "selectionReason": "同口径整通正确率更高", "candidates": [
            {"id": "old", "promptSha256": "a" * 64, "modelConfigSha256": "d" * 64,
             "caseSetSha256": "c" * 64, "productionMode": "reasoning-off"},
            {"id": "new", "promptSha256": "b" * 64, "modelConfigSha256": "e" * 64,
             "caseSetSha256": "c" * 64, "productionMode": "reasoning-off"},
        ]}
        self.assertIn("modelConfigSha256", "\n".join(
            baseline_guard.validate_selection(selection)))
        selection["candidates"][1]["modelConfigSha256"] = "d" * 64
        self.assertEqual([], baseline_guard.validate_selection(selection))
        selection["candidates"][0]["productionMode"] = ""
        selection["candidates"][1]["productionMode"] = ""
        self.assertIn("productionMode", "\n".join(
            baseline_guard.validate_selection(selection)))

    def test_batch_adjudication_records_corrections_and_exclusions_separately(self):
        decisions = {"schemaVersion": 1, "batchId": "batch-1", "reviewedBy": "user",
                     "reviewedAt": "2026-09-18T10:00:00+08:00", "decisions": [
            {"caseId": "a", "action": "keep-human", "reason": "人工口径正确"},
            {"caseId": "b", "action": "accept-model", "reason": "模型符合新定义",
             "approvedExpected": {"label": "B"}},
            {"caseId": "c", "action": "exclude", "reason": "不在本轮统计口径",
             "exclusionScope": "all-future-runs"},
        ]}
        with tempfile.TemporaryDirectory() as root:
            adjudication_path = os.path.join(root, "case_adjudication_registry.json")
            exclusion_path = os.path.join(root, "case_exclusions_registry.json")
            result = case_adjudication.apply_decisions(
                decisions, adjudication_path, exclusion_path)
            with open(adjudication_path, encoding="utf-8") as stream:
                adjudication = json.load(stream)
            with open(exclusion_path, encoding="utf-8") as stream:
                exclusions = json.load(stream)
        self.assertEqual(3, result["recorded"])
        self.assertEqual({"a", "b"}, {row["caseId"] for row in adjudication["entries"]})
        self.assertEqual(["c"], [row["caseId"] for row in exclusions["entries"]])

    def test_batch_adjudication_rolls_back_both_registries_if_second_commit_fails(self):
        decisions = {"schemaVersion": 1, "batchId": "batch-1", "reviewedBy": "user",
                     "reviewedAt": "2026-09-18T10:00:00+08:00", "decisions": [
            {"caseId": "a", "action": "keep-human", "reason": "人工口径正确"},
            {"caseId": "b", "action": "exclude", "reason": "不在统计范围",
             "exclusionScope": "current-baseline"},
        ]}
        with tempfile.TemporaryDirectory() as root:
            adjudication_path = os.path.join(root, "adjudication.json")
            exclusion_path = os.path.join(root, "exclusions.json")
            original_replace = case_adjudication.os.replace
            calls = {"count": 0}

            def fail_second(source, target):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated second registry failure")
                return original_replace(source, target)

            with mock.patch.object(case_adjudication.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    case_adjudication.apply_decisions(
                        decisions, adjudication_path, exclusion_path)
            self.assertFalse(os.path.exists(adjudication_path))
            self.assertFalse(os.path.exists(exclusion_path))

    def test_batch_adjudication_cleans_staged_file_if_second_stage_fails(self):
        decisions = {"schemaVersion": 1, "batchId": "batch-1", "reviewedBy": "user",
                     "reviewedAt": "2026-09-18T10:00:00+08:00", "decisions": [
            {"caseId": "a", "action": "keep-human", "reason": "人工口径正确"},
        ]}
        with tempfile.TemporaryDirectory() as root:
            original_stage = case_adjudication._stage_json
            calls = {"count": 0}

            def fail_second(path, value):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated staging failure")
                return original_stage(path, value)

            with mock.patch.object(case_adjudication, "_stage_json", side_effect=fail_second):
                with self.assertRaises(OSError):
                    case_adjudication.apply_decisions(
                        decisions, os.path.join(root, "adjudication.json"),
                        os.path.join(root, "exclusions.json"))
            leaked = [name for name in os.listdir(root)
                      if name.startswith(".case-adjudication-")]
        self.assertEqual([], leaked)

    def test_root_cause_analysis_requires_earliest_divergence_and_ablation(self):
        analysis = {"schemaVersion": 1, "rootCause": "开始标签先错",
                    "earliestDivergence": {"stage": "start-label", "evidence": "第2句首次误分",
                                           "downstreamEffects": ["结束标签被错误分支截断"]},
                    "ablation": {"factor": "已打业务标签", "constantsHeld": ["模型", "提示词", "温度"],
                                 "baselineResult": "错误", "counterfactualResult": "仍错误"}}
        self.assertEqual([], workflow_policy.validate_root_cause_analysis(analysis))
        analysis["ablation"].pop("counterfactualResult")
        self.assertIn("counterfactualResult", "\n".join(
            workflow_policy.validate_root_cause_analysis(analysis)))


if __name__ == "__main__":
    unittest.main()
