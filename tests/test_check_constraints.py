# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)
MODULE_PATH = os.path.join(BIN, "check_constraints.py")

import model_profiles
import profile_constraints


def load_module(testcase):
    testcase.assertTrue(os.path.isfile(MODULE_PATH), "缺少语义约束验证器")
    spec = importlib.util.spec_from_file_location("check_constraints", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConstraintCheckerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry = model_profiles.load_registry()
        profile = next(item for item in registry["profiles"]
                       if item["id"] == "qwen-nonthinking")
        cls.snapshot = profile_constraints.build_constraint_snapshot(
            profile, status="accepted", accepted_ids=["QWEN-NONTHINK-01"],
            overrides={}, accepted_at="2026-09-08T10:00:00Z",
            principle_ids=[], principles_confirmed=True)
        cls.compliance = [{
            "id": "QWEN-NONTHINK-01", "status": "PASS", "evidence": "规则表第1行"
        }]

    def test_unverifiable_semantic_constraint_fails_closed(self):
        module = load_module(self)
        result = module.combine_results([], [{
            "id": "QWEN-NONTHINK-01", "verdict": "UNVERIFIABLE",
            "evidence": "", "reason": "无法解析",
        }])
        self.assertEqual("FAIL", result["verdict"])

    def test_static_compliance_error_skips_judge_and_fails(self):
        module = load_module(self)
        original = module.llm.chat
        module.llm.chat = lambda *args, **kwargs: self.fail("静态门禁失败后不应调用 Judge")
        try:
            result = module.validate_candidate(
                {"models": {"judge": {"model": "judge"}}}, self.snapshot,
                "候选提示词", [], timeout=1)
        finally:
            module.llm.chat = original
        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(result["staticErrors"])
        self.assertEqual([], result["results"])

    def test_semantic_pass_returns_constraint_evidence(self):
        module = load_module(self)
        original = module.llm.chat
        captured = {}

        def fake_chat(_config, _role, messages, **kwargs):
            captured["messages"] = messages
            return {"content": '{"verdict":"PASS","evidence":"规则表第1行","reason":"条件和动作相邻"}'}

        module.llm.chat = fake_chat
        try:
            result = module.validate_candidate(
                {"models": {"judge": {"model": "judge"}}}, self.snapshot,
                "若条件A成立，立即执行动作B。", self.compliance, timeout=1)
        finally:
            module.llm.chat = original
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual("QWEN-NONTHINK-01", result["results"][0]["id"])
        self.assertEqual("规则表第1行", result["results"][0]["evidence"])
        self.assertEqual(["system", "user"], [item["role"] for item in captured["messages"]])
        self.assertNotIn("若条件A成立", captured["messages"][0]["content"])
        self.assertIn("若条件A成立", captured["messages"][1]["content"])

    def test_malformed_judge_output_becomes_unverifiable(self):
        module = load_module(self)
        original = module.llm.chat
        module.llm.chat = lambda *args, **kwargs: {"content": "不是JSON"}
        try:
            result = module.validate_candidate(
                {"models": {"judge": {"model": "judge"}}}, self.snapshot,
                "候选提示词", self.compliance, timeout=1)
        finally:
            module.llm.chat = original
        self.assertEqual("FAIL", result["verdict"])
        self.assertEqual("UNVERIFIABLE", result["results"][0]["verdict"])

    def test_rejected_profile_passes_without_semantic_calls(self):
        module = load_module(self)
        registry = model_profiles.load_registry()
        profile = next(item for item in registry["profiles"]
                       if item["id"] == "qwen-nonthinking")
        snapshot = profile_constraints.build_constraint_snapshot(
            profile, status="rejected", accepted_ids=[], overrides={},
            principle_ids=[], principles_confirmed=True)
        original = module.llm.chat
        module.llm.chat = lambda *args, **kwargs: self.fail("无硬约束时不应调用 Judge")
        try:
            result = module.validate_candidate({}, snapshot, "候选提示词", [], timeout=1)
        finally:
            module.llm.chat = original
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual([], result["results"])


if __name__ == "__main__":
    unittest.main()
