# -*- coding: utf-8 -*-
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import model_profiles

try:
    import profile_constraints
except ImportError:
    profile_constraints = None


class ProfileConstraintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = model_profiles.load_registry()
        cls.profile = next(item for item in cls.registry["profiles"]
                           if item["id"] == "qwen-nonthinking")

    def require_module(self):
        self.assertIsNotNone(profile_constraints, "缺少用户约束快照模块")

    def test_rejected_profile_has_no_hard_constraints(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="rejected", accepted_ids=[], overrides={})
        self.assertEqual([], snapshot["hardConstraints"])
        self.assertEqual("rejected", snapshot["status"])
        with self.assertRaisesRegex(ValueError, "拒绝画像"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="rejected",
                accepted_ids=["QWEN-NONTHINK-01"], overrides={})

    def test_user_modified_text_wins_over_profile_default(self):
        self.require_module()
        text = "条件满足后只调用指定函数，不输出垫词"
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="modified", accepted_ids=["QWEN-NONTHINK-01"],
            overrides={"QWEN-NONTHINK-01": text}, preamble_owner="host",
            accepted_at="2026-09-08T10:00:00Z")
        self.assertEqual(text, snapshot["hardConstraints"][0]["text"])
        self.assertEqual("required", snapshot["hardConstraints"][0]["level"])
        self.assertEqual("host", snapshot["preambleOwner"])
        self.assertEqual("host-agent", snapshot["consumer"])
        self.assertEqual("candidate", snapshot["targetRole"])
        self.assertEqual("tool-agent", snapshot["promptTaskType"])
        self.assertEqual(4, snapshot["schemaVersion"])
        self.assertEqual([{"id": "QWEN-NONTHINK-01", "text": text}],
                         snapshot["userOverrides"])

    def test_confirmed_universal_principles_merge_with_model_directives(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="accepted", accepted_ids=["QWEN-NONTHINK-01"],
            overrides={}, principle_ids=["PRINCIPLE-GLOBAL-REVIEW", "PRINCIPLE-CONCISE"],
            principles_confirmed=True)
        self.assertEqual(4, snapshot["schemaVersion"])
        self.assertTrue(snapshot["principlesConfirmed"])
        self.assertEqual(["PRINCIPLE-GLOBAL-REVIEW", "PRINCIPLE-CONCISE"],
                         snapshot["selectedPrincipleIds"])
        self.assertEqual({"universal", "model-profile"},
                         {item["sourceType"] for item in snapshot["hardConstraints"]})

    def test_unconfirmed_principle_selection_fails_snapshot_validation(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="rejected", accepted_ids=[], overrides={},
            principle_ids=["PRINCIPLE-GLOBAL-REVIEW"], principles_confirmed=False)
        errors = "\n".join(profile_constraints.validate_constraint_snapshot(
            snapshot, self.registry))
        self.assertIn("通用优化原则尚未由用户确认", errors)

    def test_selected_principle_cannot_be_missing_from_hard_constraints(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="rejected", accepted_ids=[], overrides={},
            principle_ids=["PRINCIPLE-GLOBAL-REVIEW"], principles_confirmed=True)
        snapshot["hardConstraints"] = []
        snapshot["universalConstraints"] = []
        errors = "\n".join(profile_constraints.validate_constraint_snapshot(
            snapshot, self.registry))
        self.assertIn("选择结果与硬约束不一致", errors)

    def test_tool_specific_directive_is_rejected_for_classification(self):
        self.require_module()
        with self.assertRaisesRegex(ValueError, "不适用于"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="accepted",
                accepted_ids=["QWEN-NONTHINK-03"], overrides={},
                task_type="classification")

    def test_legacy_accepted_snapshot_without_prompt_task_type_requires_migration(self):
        self.require_module()
        snapshot = {
            "schemaVersion": 1,
            "profileId": "qwen-nonthinking",
            "status": "accepted",
            "hardConstraints": [{
                "id": "QWEN-NONTHINK-01", "text": "旧约束", "level": "required"
            }],
        }
        errors = "\n".join(profile_constraints.validate_constraint_snapshot(
            snapshot, self.registry))
        self.assertIn("promptTaskType", errors)
        self.assertIn("迁移", errors)

    def test_unknown_constraint_id_is_rejected(self):
        self.require_module()
        with self.assertRaisesRegex(ValueError, "未知画像约束"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="accepted", accepted_ids=["MISSING"], overrides={})

    def test_override_for_unselected_constraint_is_rejected(self):
        self.require_module()
        with self.assertRaisesRegex(ValueError, "尚未选择"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="modified", accepted_ids=["QWEN-NONTHINK-01"],
                overrides={"QWEN-NONTHINK-02": "用户修改文本"})

    def test_invalid_status_and_preamble_owner_are_rejected(self):
        self.require_module()
        with self.assertRaisesRegex(ValueError, "status"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="pending", accepted_ids=[], overrides={})
        with self.assertRaisesRegex(ValueError, "preambleOwner"):
            profile_constraints.build_constraint_snapshot(
                self.profile, status="accepted",
                accepted_ids=["QWEN-NONTHINK-01"], overrides={},
                preamble_owner="both")

    def test_snapshot_validation_detects_unknown_profile_and_empty_acceptance(self):
        self.require_module()
        snapshot = {
            "schemaVersion": 1, "profileId": "missing", "status": "accepted",
            "hardConstraints": [], "preambleOwner": None,
        }
        errors = profile_constraints.validate_constraint_snapshot(snapshot, self.registry)
        self.assertTrue(any("未知模型画像" in item for item in errors))
        self.assertTrue(any("至少一个硬约束" in item for item in errors))

    def test_missing_compliance_for_any_hard_constraint_fails(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="accepted",
            accepted_ids=["QWEN-NONTHINK-01", "QWEN-NONTHINK-02"], overrides={})
        compliance = [{
            "id": "QWEN-NONTHINK-01", "status": "PASS", "evidence": "第3节"
        }]
        errors = profile_constraints.validate_compliance(snapshot, compliance)
        self.assertTrue(any("QWEN-NONTHINK-02" in item for item in errors))

    def test_nonpass_empty_evidence_duplicate_and_unknown_compliance_fail(self):
        self.require_module()
        snapshot = profile_constraints.build_constraint_snapshot(
            self.profile, status="accepted",
            accepted_ids=["QWEN-NONTHINK-01"], overrides={})
        compliance = [
            {"id": "QWEN-NONTHINK-01", "status": "FAIL", "evidence": ""},
            {"id": "QWEN-NONTHINK-01", "status": "PASS", "evidence": "第3节"},
            {"id": "EXTRA", "status": "PASS", "evidence": "无"},
        ]
        errors = "\n".join(profile_constraints.validate_compliance(snapshot, compliance))
        self.assertIn("重复", errors)
        self.assertIn("未通过", errors)
        self.assertIn("证据为空", errors)
        self.assertIn("未知", errors)


if __name__ == "__main__":
    unittest.main()
