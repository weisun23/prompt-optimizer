# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


def load_module(testcase, name):
    path = os.path.join(BIN, name + ".py")
    testcase.assertTrue(os.path.isfile(path), "缺少 %s.py" % name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def triage_item(case_id, owner="model_failure", evidence=None, action="加入问题集", **extra):
    item = {
        "caseId": case_id, "caseStatus": "pending_review" if owner == "case_error" else "active",
        "failureOwner": owner, "confidence": "high", "taxonomyValues": {"scenario": "default"},
        "stabilityEvidence": {"validRuns": 0, "failureRuns": 0, "passRuns": 0,
                              "invalidRuns": 0, "environmentSha256": ""},
        "stabilityLabel": "not_retested", "optimizationEligible": owner == "model_failure",
        "evidence": (["输入事实、期望定义与完整上下文已核对"]
                     if evidence is None else evidence),
        "authoritativeBasis": ["任务正式定义"], "contextScope": "full",
        "neighborCaseIds": ["neighbor-1"],
        "decisionAnchor": "not-applicable：单次输出任务",
        "fieldsUsed": [], "fieldProvenance": [], "action": action,
    }
    item.update(extra)
    return item


class FailureTriageTest(unittest.TestCase):
    def test_cli_requires_scores_for_complete_failure_coverage(self):
        module = load_module(self, "failure_triage")
        with mock.patch.object(sys, "argv", ["failure_triage.py", "--triage", "triage.json"]):
            with self.assertRaises(SystemExit) as raised:
                module.main()
        self.assertEqual(2, raised.exception.code)

    def test_case_error_requires_evidence_proposal_and_pending_review(self):
        module = load_module(self, "failure_triage")
        triage = triage_doc([triage_item(
            "case-1", "case_error", evidence=[], action="修正用例",
            authoritativeBasis=[], neighborCaseIds=[], decisionAnchor="")])
        errors = "\n".join(module.validate_triage(triage))
        for term in ("evidence", "authoritativeBasis", "neighborCaseIds", "decisionAnchor"):
            self.assertIn(term, errors)
        self.assertIn("proposedCasePath", errors)
        self.assertIn("pending_review", errors)

    def test_only_confirmed_model_failures_enter_problem_set(self):
        module = load_module(self, "failure_triage")
        triage = triage_doc([
            triage_item("model-1"),
            triage_item("case-1", "case_error", action="等待人工审核",
                        proposedCasePath="proposed-corrections/case-1.json",
                        reviewStatus="pending_review"),
        ])
        self.assertEqual([], module.validate_triage(triage))
        self.assertEqual(["model-1"], module.model_failure_case_ids(triage))

    def test_neighbor_cases_must_exist_and_be_current_passes(self):
        module = load_module(self, "failure_triage")
        triage = triage_doc([triage_item("model-1")])
        valid_scores = {
            "model-1": {"caseId": "model-1", "verdict": "FAIL"},
            "neighbor-1": {"caseId": "neighbor-1", "verdict": "PASS"},
        }
        self.assertEqual([], module.validate_triage(
            triage, failed_case_ids={"model-1"}, score_rows=valid_scores))
        invalid_scores = dict(valid_scores, **{
            "neighbor-1": {"caseId": "neighbor-1", "verdict": "FAIL"}})
        errors = "\n".join(module.validate_triage(
            triage, failed_case_ids={"model-1", "neighbor-1"}, score_rows=invalid_scores))
        self.assertIn("neighborCaseIds", errors)
        self.assertIn("PASS", errors)


class CaseCorrectionGuardTest(unittest.TestCase):
    def test_cli_requires_receipt_path_before_applying_changes(self):
        module = load_module(self, "case_correction_guard")
        with mock.patch.object(sys, "argv", [
                "case_correction_guard.py", "--review", "review.json",
                "--case-root", "cases"]):
            with self.assertRaises(SystemExit) as raised:
                module.main()
        self.assertEqual(2, raised.exception.code)

    def make_fixture(self, root, status="approved"):
        case_root = os.path.join(root, "cases")
        case_path = os.path.join(case_root, "generated", "case-1.json")
        proposal_path = os.path.join(case_root, "proposed-corrections", "case-1.json")
        triage_path = os.path.join(case_root, "failure-triage.json")
        write_json(case_path, {"id": "case-1", "expectedOutput": "错误值"})
        write_json(proposal_path, {"id": "case-1", "expectedOutput": "正确值"})
        write_json(triage_path, triage_doc([triage_item(
            "case-1", "case_error", action="等待人工审核",
            proposedCasePath="proposed-corrections/case-1.json",
            reviewStatus="pending_review")]))
        review = {"schemaVersion": 2, "triagePath": "failure-triage.json",
                  "triageSha256": file_hash(triage_path),
                  "approvalStatement": "我已核对原用例、修正提案和差异，同意应用。",
                  "reviews": [{
            "caseId": "case-1", "casePath": "generated/case-1.json",
            "proposalPath": "proposed-corrections/case-1.json",
            "reviewStatus": status, "reviewedBy": "user",
            "reviewedAt": "2026-09-15T10:00:00+08:00",
            "originalSha256": file_hash(case_path), "proposedSha256": file_hash(proposal_path),
            "reason": "期望结果与输入事实冲突",
        }]}
        review_path = os.path.join(case_root, "case-correction-review.json")
        write_json(review_path, review)
        return case_root, case_path, review_path

    def test_review_requires_untampered_triage_chain(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            write_json(os.path.join(case_root, "failure-triage.json"), {
                "schemaVersion": 3, "taxonomy": taxonomy(), "items": []})
            before = file_hash(case_path)
            with self.assertRaisesRegex(ValueError, "归因文件哈希"):
                module.apply_review(review_path, case_root)
            self.assertEqual(before, file_hash(case_path))

    def test_review_rejects_invalid_triage_even_when_hash_matches(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            triage_path = os.path.join(case_root, "failure-triage.json")
            with open(triage_path, encoding="utf-8") as stream:
                triage = json.load(stream)
            triage["items"].append(dict(triage["items"][0]))
            write_json(triage_path, triage)
            with open(review_path, encoding="utf-8") as stream:
                review = json.load(stream)
            review["triageSha256"] = file_hash(triage_path)
            write_json(review_path, review)
            before = file_hash(case_path)
            with self.assertRaisesRegex(ValueError, "caseId 重复"):
                module.apply_review(review_path, case_root)
            self.assertEqual(before, file_hash(case_path))

    def test_review_rejects_malformed_triage_case_id_without_crashing(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            triage_path = os.path.join(case_root, "failure-triage.json")
            with open(triage_path, encoding="utf-8") as stream:
                triage = json.load(stream)
            triage["items"].append({"caseId": ["bad"]})
            write_json(triage_path, triage)
            with open(review_path, encoding="utf-8") as stream:
                review = json.load(stream)
            review["triageSha256"] = file_hash(triage_path)
            write_json(review_path, review)
            before = file_hash(case_path)
            with self.assertRaisesRegex(ValueError, "caseId"):
                module.apply_review(review_path, case_root)
            self.assertEqual(before, file_hash(case_path))

    def test_review_rejects_non_string_case_path_without_traceback(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            with open(review_path, encoding="utf-8") as stream:
                review = json.load(stream)
            review["reviews"][0]["casePath"] = ["bad"]
            write_json(review_path, review)
            before = file_hash(case_path)
            with self.assertRaisesRegex(ValueError, "用例路径"):
                module.apply_review(review_path, case_root)
            self.assertEqual(before, file_hash(case_path))

    def test_case_set_hash_ignores_review_metadata(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, _, _ = self.make_fixture(root)
            before = module.case_set_hash(case_root)
            write_json(os.path.join(case_root, "failure-triage.json"), {
                "schemaVersion": 3, "taxonomy": taxonomy(), "items": [{"caseId": "metadata-only"}]})
            after = module.case_set_hash(case_root)
        self.assertEqual(before, after)

    def test_pending_review_cannot_modify_official_case(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root, "pending_review")
            before = file_hash(case_path)
            with self.assertRaisesRegex(ValueError, "人工批准"):
                module.apply_review(review_path, case_root)
            self.assertEqual(before, file_hash(case_path))

    def test_approved_review_applies_once_and_requires_rebaseline(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            receipt = module.apply_review(review_path, case_root)
            with open(case_path, encoding="utf-8") as stream:
                corrected = json.load(stream)
            second = module.apply_review(review_path, case_root)
            registry_path = os.path.join(case_root, "case_corrections_registry.json")
            marker_path = os.path.join(case_root, ".rebaseline-required.json")
            with open(registry_path, encoding="utf-8") as stream:
                registry = json.load(stream)
            with open(marker_path, encoding="utf-8") as stream:
                marker = json.load(stream)
        self.assertEqual("正确值", corrected["expectedOutput"])
        self.assertEqual(["case-1"], receipt["appliedCaseIds"])
        self.assertTrue(receipt["requiresRebaseline"])
        self.assertEqual(1, receipt["caseCount"])
        self.assertEqual(["case-1"], receipt["caseIds"])
        self.assertEqual(["case-1"], second["skippedAlreadyApproved"])
        self.assertTrue(second["requiresRebaseline"])
        self.assertEqual(1, len(registry["entries"]))
        self.assertEqual("pending", marker["status"])
        self.assertEqual(receipt["caseSetSha256"], marker["caseSetSha256"])

    def test_changed_corrected_case_requires_new_human_review(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            module.apply_review(review_path, case_root)
            write_json(case_path, {"id": "case-1", "expectedOutput": "再次变化"})
            with self.assertRaisesRegex(ValueError, "重新审核"):
                module.apply_review(review_path, case_root)

    def test_receipt_write_failure_rolls_back_cases_registry_and_marker(self):
        module = load_module(self, "case_correction_guard")
        with tempfile.TemporaryDirectory() as root:
            case_root, case_path, review_path = self.make_fixture(root)
            receipt_path = os.path.join(root, "receipt.json")
            original_write = module._write_atomic

            def fail_receipt(path, value):
                if os.path.abspath(path) == os.path.abspath(receipt_path):
                    raise OSError("simulated receipt write error")
                return original_write(path, value)

            with mock.patch.object(module, "_write_atomic", side_effect=fail_receipt):
                with self.assertRaises(OSError):
                    module.apply_review(review_path, case_root, receipt_path=receipt_path)
            with open(case_path, encoding="utf-8") as stream:
                restored = json.load(stream)
            self.assertFalse(os.path.exists(os.path.join(case_root, "case_corrections_registry.json")))
            self.assertFalse(os.path.exists(os.path.join(case_root, ".rebaseline-required.json")))
            self.assertFalse(os.path.exists(receipt_path))
        self.assertEqual("错误值", restored["expectedOutput"])


if __name__ == "__main__":
    unittest.main()
