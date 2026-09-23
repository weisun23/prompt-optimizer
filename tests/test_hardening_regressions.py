# -*- coding: utf-8 -*-
import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import case_correction_guard
import check_prompt
import failure_triage
import iteration_guard
import model_profiles
import profile_constraints
import runner


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def taxonomy():
    return {"schemaVersion": 1, "source": "user-confirmed", "dimensions": [{
        "id": "scenario", "name": "场景", "purpose": "测试分类", "required": True,
        "values": [{"id": "default", "name": "默认", "assignmentRule": "默认测试场景"}],
    }], "stabilityPolicy": {"source": "user-confirmed", "labels": [
        {"id": "not_retested", "name": "尚未复测", "assignmentRule": "没有复测证据"},
    ]}}


def triage_doc(items):
    return {"schemaVersion": 3, "taxonomy": taxonomy(), "items": items}


def triage_item(case_id, owner="model_failure", **extra):
    item = {
        "caseId": case_id, "caseStatus": "pending_review" if owner == "case_error" else "active",
        "failureOwner": owner, "confidence": "high", "taxonomyValues": {"scenario": "default"},
        "stabilityEvidence": {"validRuns": 0, "failureRuns": 0, "passRuns": 0,
                              "invalidRuns": 0, "environmentSha256": ""},
        "stabilityLabel": "not_retested", "optimizationEligible": owner == "model_failure",
        "evidence": ["完整证据"], "authoritativeBasis": ["正式任务定义"],
        "contextScope": "full", "neighborCaseIds": ["neighbor-1"],
        "decisionAnchor": "not-applicable：单次输出任务",
        "fieldsUsed": [], "fieldProvenance": [], "action": "加入问题集",
    }
    item.update(extra)
    return item


def integration_contract():
    return {"newRequirement": "修复x", "searchedLocations": ["x"],
            "existingRuleLocations": ["x"], "sharedDecisionAxis": "x",
            "strategy": "merge-existing", "targetLocations": ["x"],
            "rulesReplacedOrRemoved": [], "newRuleJustification": "",
            "expectedStructureEffect": "合并到x"}


def review_row(case_root, case_id, name):
    case_path = os.path.join(case_root, "generated", name + ".json")
    proposal_path = os.path.join(case_root, "proposed-corrections", name + ".json")
    write_json(case_path, {"id": case_id, "value": "old"})
    write_json(proposal_path, {"id": case_id, "value": "new"})
    return {"caseId": case_id, "casePath": "generated/%s.json" % name,
            "proposalPath": "proposed-corrections/%s.json" % name,
            "reviewStatus": "approved", "reviewedBy": "user",
            "reviewedAt": "2026-09-15T10:00:00+08:00",
            "originalSha256": file_hash(case_path), "proposedSha256": file_hash(proposal_path),
            "reason": "修正错误期望"}


def write_review(case_root, review_path, rows):
    triage_path = os.path.join(case_root, "failure-triage.json")
    write_json(triage_path, triage_doc([triage_item(
        row["caseId"], "case_error", evidence=[row["reason"]], action="等待人工审核",
        proposedCasePath=row["proposalPath"], reviewStatus="pending_review")
        for row in rows]))
    write_json(review_path, {"schemaVersion": 2, "triagePath": "failure-triage.json",
               "triageSha256": file_hash(triage_path),
               "approvalStatement": "我已核对原用例、修正提案和差异，同意应用。",
               "reviews": rows})


class HardeningRegressionTest(unittest.TestCase):
    def test_deep_case_path_still_finds_rebaseline_marker(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            nested = os.path.join(case_root, "a", "b", "c", "d", "e", "f")
            os.makedirs(nested)
            marker_path = os.path.join(case_root, ".rebaseline-required.json")
            write_json(marker_path, {"schemaVersion": 1, "status": "pending"})
            found_root, found_marker = runner._find_rebaseline_marker(
                os.path.join(nested, "case.json"))
        self.assertEqual(os.path.abspath(case_root), found_root)
        self.assertEqual(os.path.abspath(marker_path), found_marker)

    def test_backup_directory_symlink_cannot_escape_case_root(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            row = review_row(case_root, "case-1", "case-1")
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, [row])
            outside = os.path.join(root, "outside")
            os.makedirs(outside)
            backup_link = os.path.join(case_root, "correction-backups")
            try:
                os.symlink(outside, backup_link, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest("当前系统无法创建目录符号链接：%s" % error)
            with self.assertRaisesRegex(ValueError, "路径越界"):
                case_correction_guard.apply_review(review_path, case_root)
            self.assertEqual([], os.listdir(outside))

    def test_poisoned_existing_backup_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            row = review_row(case_root, "case-1", "case-1")
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, [row])
            backup_dir = os.path.join(case_root, "correction-backups")
            backup_path = os.path.join(
                backup_dir, case_correction_guard._backup_filename("case-1", row["originalSha256"]))
            write_json(backup_path, {"id": "case-1", "value": "POISON"})
            with self.assertRaisesRegex(ValueError, "备份文件哈希"):
                case_correction_guard.apply_review(review_path, case_root)
            with open(os.path.join(case_root, "generated", "case-1.json"), encoding="utf-8") as stream:
                value = json.load(stream)["value"]
        self.assertEqual("old", value)

    def test_symlink_cannot_escape_case_root(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            outside = os.path.join(root, "outside")
            os.makedirs(os.path.join(case_root, "generated"), exist_ok=True)
            os.makedirs(outside, exist_ok=True)
            link = os.path.join(case_root, "generated", "linked")
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest("当前系统无法创建目录符号链接：%s" % error)
            write_json(os.path.join(outside, "case-1.json"), {"id": "case-1", "value": "old"})
            proposal_path = os.path.join(case_root, "proposed-corrections", "case-1.json")
            write_json(proposal_path, {"id": "case-1", "value": "new"})
            row = {"caseId": "case-1", "casePath": "generated/linked/case-1.json",
                   "proposalPath": "proposed-corrections/case-1.json",
                   "reviewStatus": "approved", "reviewedBy": "user",
                   "reviewedAt": "2026-09-15T10:00:00+08:00",
                   "originalSha256": file_hash(os.path.join(outside, "case-1.json")),
                   "proposedSha256": file_hash(proposal_path), "reason": "修正错误期望"}
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, [row])
            with self.assertRaisesRegex(ValueError, "路径越界"):
                case_correction_guard.apply_review(review_path, case_root)

    def test_case_id_cannot_escape_backup_directory(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            row = review_row(case_root, "../../escaped", "case-1")
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, [row])
            receipt = case_correction_guard.apply_review(review_path, case_root)
            escaped = [name for name in os.listdir(root) if name.startswith("escaped-")]
            backups = os.listdir(os.path.join(case_root, "correction-backups"))
        self.assertEqual([], escaped)
        self.assertEqual(1, len(backups))
        self.assertEqual(["../../escaped"], receipt["appliedCaseIds"])

    def test_batch_write_failure_rolls_back_all_cases(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            rows = [review_row(case_root, "case-1", "case-1"),
                    review_row(case_root, "case-2", "case-2")]
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, rows)
            original_replace = case_correction_guard.os.replace

            def fail_second(source, target):
                if target.endswith("case-2.json"):
                    raise OSError("simulated disk error")
                return original_replace(source, target)

            with mock.patch.object(case_correction_guard.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    case_correction_guard.apply_review(review_path, case_root)
            values = []
            for name in ("case-1", "case-2"):
                with open(os.path.join(case_root, "generated", name + ".json"), encoding="utf-8") as stream:
                    values.append(json.load(stream)["value"])
            registry_exists = os.path.exists(os.path.join(case_root, "case_corrections_registry.json"))
        self.assertEqual(["old", "old"], values)
        self.assertFalse(registry_exists)

    def test_batch_staging_failure_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            rows = [review_row(case_root, "case-1", "case-1"),
                    review_row(case_root, "case-2", "case-2")]
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, rows)
            original_stage = case_correction_guard._stage_json
            calls = {"count": 0}

            def fail_second(path, value):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("simulated staging error")
                return original_stage(path, value)

            with mock.patch.object(case_correction_guard, "_stage_json", side_effect=fail_second):
                with self.assertRaises(OSError):
                    case_correction_guard.apply_review(review_path, case_root)
            leaked = []
            for directory, _, names in os.walk(case_root):
                leaked.extend(os.path.join(directory, name) for name in names
                              if name.startswith(".case-stage-"))
        self.assertEqual([], leaked)

    def test_keyboard_interrupt_rolls_back_committed_case_and_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            row = review_row(case_root, "case-1", "case-1")
            review_path = os.path.join(case_root, "review.json")
            write_review(case_root, review_path, [row])
            marker_path = os.path.join(case_root, ".rebaseline-required.json")
            original_write = case_correction_guard._write_atomic

            def interrupt_marker(path, value):
                if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(marker_path)):
                    raise KeyboardInterrupt()
                return original_write(path, value)

            with mock.patch.object(case_correction_guard, "_write_atomic", side_effect=interrupt_marker):
                with self.assertRaises(KeyboardInterrupt):
                    case_correction_guard.apply_review(review_path, case_root)
            with open(os.path.join(case_root, "generated", "case-1.json"), encoding="utf-8") as stream:
                value = json.load(stream)["value"]
            registry_exists = os.path.exists(os.path.join(case_root, "case_corrections_registry.json"))
            marker_exists = os.path.exists(marker_path)
        self.assertEqual("old", value)
        self.assertFalse(registry_exists)
        self.assertFalse(marker_exists)

    def test_iteration_guard_rejects_nan_strings_and_malformed_rows(self):
        plan = {"schemaVersion": 3, "workflowId": "evidence-driven-single-hypothesis",
                "rootCauseHypothesis": "x", "changeMode": "local",
                "architectureTrack": "production-candidate", "changeScope": ["x"],
                "requirementIntegration": integration_contract(),
                "problemCaseIds": ["f"], "neighborPassCaseIds": ["p"],
                "boundaryPairs": [{"problemCaseId": "f", "neighborPassCaseIds": ["p"],
                                   "structuralDifference": "结构边界"}],
                "repeatRuns": 3, "unstableRepeatRuns": 5,
                "acceptancePolicy": {"schemaVersion": 1, "mode": "deterministic",
                                     "source": "user-confirmed", "successCriteria": "全部通过",
                                     "criticalCaseIds": [], "protectedCaseIds": ["p"]}}
        malformed = {"problemRuns": [None, {"failedCaseIds": "bad"}],
                     "neighborRuns": [{"regressedCaseIds": "bad"}, {}],
                     "criticalFailures": [],
                     "stableAcrossRuns": True,
                     "fullRegression": {"completed": True, "unacceptableRegression": "true"}}
        errors = "\n".join(iteration_guard.validate_acceptance(plan, malformed))
        self.assertIn("failedCaseIds", errors)
        self.assertIn("regressedCaseIds", errors)
        self.assertIn("全量回归", errors)

    def test_iteration_guard_rejects_malformed_top_level_objects_without_crashing(self):
        plan = {"schemaVersion": 3, "workflowId": "evidence-driven-single-hypothesis",
                "rootCauseHypothesis": "x", "changeMode": "local",
                "architectureTrack": "production-candidate", "changeScope": ["x"],
                "requirementIntegration": integration_contract(),
                "problemCaseIds": ["f"], "neighborPassCaseIds": ["p"],
                "boundaryPairs": [{"problemCaseId": "f", "neighborPassCaseIds": ["p"],
                                   "structuralDifference": "结构边界"}],
                "repeatRuns": 3, "unstableRepeatRuns": 5,
                "acceptancePolicy": "invalid"}
        plan_errors = "\n".join(iteration_guard.validate_plan(plan))
        self.assertIn("acceptancePolicy", plan_errors)
        valid_plan = dict(plan, acceptancePolicy={"schemaVersion": 1, "mode": "deterministic",
                          "source": "user-confirmed", "successCriteria": "全部通过",
                          "criticalCaseIds": [], "protectedCaseIds": ["p"]})
        result_errors = "\n".join(iteration_guard.validate_acceptance(valid_plan, []))
        self.assertIn("结果格式", result_errors)

    def test_iteration_guard_rejects_malformed_repeat_and_boundary_ids_without_crashing(self):
        plan = {"schemaVersion": 3, "workflowId": "evidence-driven-single-hypothesis",
                "rootCauseHypothesis": "x", "changeMode": "local",
                "architectureTrack": "production-candidate", "changeScope": ["x"],
                "requirementIntegration": integration_contract(),
                "problemCaseIds": [{}], "neighborPassCaseIds": [["p"]],
                "boundaryPairs": [{"problemCaseId": {}, "neighborPassCaseIds": [["p"]],
                                   "structuralDifference": "x"}],
                "repeatRuns": "3", "unstableRepeatRuns": 5,
                "acceptancePolicy": {"schemaVersion": 1, "mode": "deterministic",
                                     "source": "user-confirmed", "successCriteria": "全部通过",
                                     "criticalCaseIds": [], "protectedCaseIds": ["p"]}}
        errors = "\n".join(iteration_guard.validate_plan(plan))
        self.assertIn("repeatRuns", errors)
        self.assertIn("CaseIds", errors)

    def test_spoofed_shrink_snapshot_is_rejected(self):
        spoof = {"schemaVersion": 3, "principlesConfirmed": True,
                 "selectedPrincipleIds": ["PRINCIPLE-CONCISE"], "hardConstraints": []}
        errors = check_prompt.shrink_authorization_errors(True, spoof)
        self.assertTrue(errors)

    def test_all_legacy_constraint_snapshots_require_migration(self):
        registry = model_profiles.load_registry()
        legacy = {"schemaVersion": 1, "profileId": "qwen-nonthinking",
                  "status": "rejected", "hardConstraints": []}
        errors = "\n".join(profile_constraints.validate_constraint_snapshot(legacy, registry))
        self.assertIn("迁移", errors)
        old_v3 = {"schemaVersion": 3, "profileId": "qwen-nonthinking",
                  "status": "rejected", "hardConstraints": [], "promptTaskType": "classification",
                  "principlesConfirmed": True, "selectedPrincipleIds": [],
                  "selectedModelDirectiveIds": []}
        self.assertIn("迁移", "\n".join(
            profile_constraints.validate_constraint_snapshot(old_v3, registry)))

    def test_triage_must_cover_every_failed_case(self):
        triage = triage_doc([triage_item("a")])
        errors = "\n".join(failure_triage.validate_triage(triage, failed_case_ids={"a", "b"}))
        self.assertIn("b", errors)

    def test_failure_triage_rejects_malformed_evidence_contract_without_crashing(self):
        malformed = {"schemaVersion": 3, "taxonomy": taxonomy(), "items": [{
            "caseId": "case-1", "failureOwner": "model_failure", "confidence": "high",
            "evidence": [{}], "authoritativeBasis": [{}], "contextScope": "full",
            "neighborCaseIds": [{}], "decisionAnchor": "x", "fieldsUsed": [{}],
            "fieldProvenance": [{"field": ["bad"]}], "action": "x",
        }, {"caseId": ["bad"]}]}
        errors = "\n".join(failure_triage.validate_triage(
            malformed, failed_case_ids={"case-1"},
            score_rows={"neighbor": {"verdict": "PASS"}}))
        self.assertIn("caseId", errors)
        self.assertIn("neighborCaseIds", errors)
        self.assertIn("fieldsUsed", errors)

    def test_runner_resolves_profile_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            run = os.path.join(root, "run")
            out = os.path.join(run, "trajectories")
            os.makedirs(run)
            config_path = os.path.join(root, "config.json")
            task_path = os.path.join(root, "task.json")
            cases_path = os.path.join(root, "cases.json")
            prompt_path = os.path.join(run, "prompt.md")
            write_json(config_path, {"models": {"candidate": {
                "model": "demo", "promptProfile": "missing-profile"}}})
            write_json(task_path, {"initialPrompt": "fallback"})
            write_json(cases_path, [{"id": "c", "chat": {"simulateMode": "off",
                       "messages": [{"role": "user", "content": "hi"}]}}])
            with open(prompt_path, "w", encoding="utf-8") as stream:
                stream.write("system")
            argv = ["runner.py", "--config", config_path, "--task", task_path,
                    "--prompt", prompt_path, "--cases", cases_path, "--out", out]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", side_effect=AssertionError("不应执行")):
                with self.assertRaisesRegex(ValueError, "未知模型画像"):
                    runner.main()

    def test_minimal_fake_constraint_receipt_is_rejected(self):
        registry = model_profiles.load_registry()
        profile = next(item for item in registry["profiles"] if item["id"] == "qwen-nonthinking")
        snapshot = profile_constraints.build_constraint_snapshot(
            profile, "accepted", ["QWEN-NONTHINK-01"], {},
            principle_ids=["PRINCIPLE-GLOBAL-REVIEW"], principles_confirmed=True)
        fake = {"verdict": "PASS", "promptSha256": "a" * 64,
                "constraintsSha256": "b" * 64}
        errors = "\n".join(runner.constraint_validation_errors(snapshot, fake))
        for term in ("schemaVersion", "validatedAt", "staticErrors", "results", "judge"):
            self.assertIn(term, errors)
        constraint_id = snapshot["hardConstraints"][0]["id"]
        duplicate = {"schemaVersion": 1, "verdict": "PASS", "validatedAt": "2026-09-15T00:00:00Z",
                     "staticErrors": [], "judge": {"model": "judge"},
                     "profileId": snapshot["profileId"], "results": [
                         {"id": constraint_id, "verdict": "PASS", "evidence": "a"},
                         {"id": constraint_id, "verdict": "PASS", "evidence": "b"},
                     ]}
        duplicate_errors = "\n".join(runner.constraint_validation_errors(snapshot, duplicate))
        self.assertIn("重复", duplicate_errors)

    def test_rebaseline_gate_requires_full_run_and_matching_receipt(self):
        marker = {"schemaVersion": 1, "status": "pending", "caseSetSha256": "a" * 64,
                  "appliedCaseIds": ["case-1"], "caseCount": 1, "caseIds": ["case-1"]}
        receipt = {"schemaVersion": 1, "requiresRebaseline": True,
                   "caseSetSha256": "a" * 64, "appliedCaseIds": ["case-1"],
                   "caseCount": 1, "caseIds": ["case-1"]}
        self.assertEqual([], runner.rebaseline_gate_errors(
            marker, receipt, "full", "a" * 64, ["case-1"], False))
        errors = "\n".join(runner.rebaseline_gate_errors(
            marker, None, "regression", "b" * 64, ["case-1"], False))
        self.assertIn("修正收据", errors)
        self.assertIn("full", errors)
        self.assertIn("哈希", errors)
        partial = "\n".join(runner.rebaseline_gate_errors(
            marker, receipt, "full", "a" * 64, ["case-1"], True))
        self.assertIn("完整", partial)
        malformed = runner.rebaseline_gate_errors(
            {"schemaVersion": 1, "status": "unknown"}, None, "full", "a" * 64)
        self.assertTrue(malformed)

    def test_already_approved_case_error_is_not_triaged_again(self):
        with tempfile.TemporaryDirectory() as root:
            case_root = os.path.join(root, "cases")
            case_path = os.path.join(case_root, "generated", "case-1.json")
            write_json(case_path, {"id": "case-1", "value": "approved"})
            registry = {"schemaVersion": 1, "entries": [{
                "caseId": "case-1", "status": "applied", "casePath": "generated/case-1.json",
                "correctedSha256": file_hash(case_path),
            }]}
            triage = triage_doc([triage_item(
                "case-1", "case_error", evidence=["再次提议"], action="等待审核",
                proposedCasePath="proposed-corrections/case-1.json",
                reviewStatus="pending_review")])
            errors = "\n".join(failure_triage.validate_triage(
                triage, correction_registry=registry, case_root=case_root))
        self.assertIn("已批准修正", errors)

    def test_malformed_registry_path_does_not_crash_failure_triage(self):
        triage = triage_doc([triage_item(
            "case-1", "case_error", evidence=["需要审核"], action="等待审核",
            proposedCasePath="proposed-corrections/case-1.json",
            reviewStatus="pending_review")])
        registry = {"schemaVersion": 1, "entries": [{
            "caseId": "case-1", "status": "applied", "casePath": "Z:\\outside.json",
            "correctedSha256": "0" * 64,
        }]}
        with tempfile.TemporaryDirectory() as case_root:
            errors = failure_triage.validate_triage(
                triage, correction_registry=registry, case_root=case_root)
            malformed = failure_triage.validate_triage(
                triage, correction_registry={"entries": [None]}, case_root=case_root)
            malformed_path = failure_triage.validate_triage(
                triage, correction_registry={"entries": [{
                    "caseId": "case-1", "status": "applied", "casePath": ["bad"]
                }]}, case_root=case_root)
        self.assertEqual([], errors)
        self.assertEqual([], malformed)
        self.assertEqual([], malformed_path)


if __name__ == "__main__":
    unittest.main()
