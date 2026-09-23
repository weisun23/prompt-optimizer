# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "bin", "model_profiles.py")
REGISTRY_PATH = os.path.join(ROOT, "profiles", "model-profiles.json")


def load_module(testcase):
    testcase.assertTrue(os.path.isfile(MODULE_PATH), "缺少模型画像解析器")
    spec = importlib.util.spec_from_file_location("model_profiles", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelProfileTest(unittest.TestCase):
    def test_registry_contains_all_supported_profiles_and_unique_constraints(self):
        self.assertTrue(os.path.isfile(REGISTRY_PATH), "缺少模型画像知识库")
        with open(REGISTRY_PATH, encoding="utf-8") as stream:
            registry = json.load(stream)
        self.assertEqual(2, registry["schemaVersion"])
        principle_ids = {item["id"] for item in registry["universalOptimizationPrinciples"]}
        self.assertEqual({
            "PRINCIPLE-GLOBAL-REVIEW", "PRINCIPLE-ROOT-CAUSE-FIRST",
            "PRINCIPLE-AVOID-PATCHES", "PRINCIPLE-CONCISE",
            "PRINCIPLE-PRESERVE-PLACEHOLDERS", "PRINCIPLE-PRESERVE-CAPABILITIES",
            "PRINCIPLE-PRESERVE-FORMAT", "PRINCIPLE-PRESERVE-TOOL-INTERFACE",
            "PRINCIPLE-STRUCTURAL-EVIDENCE", "PRINCIPLE-FIELD-PROVENANCE",
            "PRINCIPLE-TEMPORAL-ANCHOR", "PRINCIPLE-BOUNDARY-PAIRS",
        }, principle_ids)
        self.assertIn("slot-filling", registry["taskTypeDirectives"])
        self.assertIn("json-output", registry["taskTypeDirectives"])
        classification_ids = {item["id"] for item in registry["taskTypeDirectives"]["classification"]}
        for directive_id in ("TASK-CLASSIFICATION-PARTITION", "TASK-CLASSIFICATION-SOURCE",
                             "TASK-CLASSIFICATION-ANCHOR"):
            self.assertIn(directive_id, classification_ids)
        ids = [item["id"] for item in registry["profiles"]]
        self.assertEqual({
            "generic", "qwen-nonthinking", "deepseek-nonthinking",
            "deepseek-thinking", "glm-thinking", "kimi-k3", "kimi-instant",
            "minimax-m", "minimax-m3-adaptive", "doubao-function-call",
        }, set(ids))
        constraint_ids = [rule["id"] for rules in registry["taskTypeDirectives"].values()
                          for rule in rules]
        for profile in registry["profiles"]:
            constraint_ids.extend(rule["id"] for rule in profile.get("commonOptimizationDirectives", []))
            constraint_ids.extend(rule["id"] for rule in profile.get("optimizationDirectives", []))
            constraint_ids.extend(rule["id"] for rules in profile.get("taskOptimizationDirectives", {}).values()
                                  for rule in rules)
        self.assertEqual(len(constraint_ids), len(set(constraint_ids)))
        for profile in registry["profiles"]:
            self.assertEqual("host-agent", profile["consumer"])
            self.assertIn("*", profile["targetRoles"])

    def test_qwen_nonthinking_matches_by_explicit_family_and_mode(self):
        module = load_module(self)
        config = {"models": {"candidate": {
            "provider": "qwen", "modelFamily": "qwen3",
            "model": "Qwen3.8-27B", "reasoning": False,
            "promptProfile": "auto",
        }}}
        result = module.resolve_profile(config)
        self.assertEqual("qwen-nonthinking", result["profile"]["id"])
        self.assertEqual("high", result["confidence"])
        self.assertIn("provider", result["matchedBy"])
        self.assertIn("modelFamily", result["matchedBy"])

    def test_deepseek_mode_selects_different_profiles(self):
        module = load_module(self)
        base = {"provider": "deepseek", "modelFamily": "deepseek-v4",
                "model": "deepseek-v4-pro", "promptProfile": "auto"}
        nonthinking = module.resolve_profile({"models": {"candidate": dict(base, reasoning=False)}})
        thinking = module.resolve_profile({"models": {"candidate": dict(base, reasoning=True)}})
        self.assertEqual("deepseek-nonthinking", nonthinking["profile"]["id"])
        self.assertEqual("deepseek-thinking", thinking["profile"]["id"])

    def test_base_url_is_not_used_to_guess_provider(self):
        module = load_module(self)
        config = {"models": {"candidate": {
            "base_url": "https://api.deepseek.example/v1", "model": "proxylk-1",
            "reasoning": False, "promptProfile": "auto",
        }}}
        result = module.resolve_profile(config)
        self.assertEqual("generic", result["profile"]["id"])
        self.assertNotIn("base_url", result["matchedBy"])

    def test_unknown_explicit_profile_is_rejected(self):
        module = load_module(self)
        config = {"models": {"candidate": {"promptProfile": "missing-profile"}}}
        with self.assertRaisesRegex(ValueError, "未知模型画像"):
            module.resolve_profile(config)

    def test_ambiguous_provider_without_family_uses_generic(self):
        module = load_module(self)
        config = {"models": {"candidate": {
            "provider": "kimi", "model": "unknown", "promptProfile": "auto",
        }}}
        result = module.resolve_profile(config)
        self.assertEqual("generic", result["profile"]["id"])
        self.assertEqual("ambiguous", result["confidence"])

    def test_judge_target_gets_host_judge_directives_without_tool_directives(self):
        module = load_module(self)
        config = {"models": {"judge": {
            "provider": "qwen", "modelFamily": "qwen3",
            "model": "Qwen3.8-27B", "reasoning": False,
            "promptProfile": "auto",
        }}}
        resolved = module.resolve_profile(config, role="judge")
        recommendation = module.build_recommendation(
            resolved["profile"], role="judge", task_type="judge")
        self.assertEqual("host-agent", recommendation["consumer"])
        directive_ids = {item["id"] for item in recommendation["hostOptimizationDirectives"]}
        self.assertIn("TASK-JUDGE-01", directive_ids)
        self.assertFalse(any("TOOL" in item for item in directive_ids))
        self.assertTrue(recommendation["runtimeRequirements"])

    def test_host_receives_qwen_common_and_task_directives_for_all_prompt_types(self):
        module = load_module(self)
        config = {"models": {"candidate": {
            "provider": "qwen", "modelFamily": "qwen3",
            "model": "Qwen3.8-27B", "reasoning": False,
            "promptProfile": "auto",
        }}}
        profile = module.resolve_profile(config)["profile"]
        tool_recommendation = module.build_recommendation(
            profile, role="candidate", task_type="tool-agent")
        classification_recommendation = module.build_recommendation(
            profile, role="candidate", task_type="classification")
        self.assertTrue(tool_recommendation["hostOptimizationDirectives"])
        self.assertTrue(classification_recommendation["hostOptimizationDirectives"])
        classification_ids = {item["id"] for item in classification_recommendation["hostOptimizationDirectives"]}
        self.assertIn("TASK-CLASSIFICATION-01", classification_ids)
        self.assertFalse(any("TOOL" in item for item in classification_ids))

    def test_prompt_task_type_is_explicit_or_inferred_without_model_guessing(self):
        module = load_module(self)
        self.assertEqual("classification", module.infer_prompt_task_type({
            "promptTaskType": "classification", "tools": [{"name": "ignored"}]
        }))
        self.assertEqual("tool-agent", module.infer_prompt_task_type({
            "tools": [{"type": "function", "function": {"name": "query"}}]
        }))
        self.assertEqual("inject-general", module.infer_prompt_task_type({"taskType": "inject"}))
        self.assertEqual("chat-general", module.infer_prompt_task_type({"taskType": "chat"}))
        self.assertEqual("slot-filling", module.infer_prompt_task_type({}, target_role="slotfiller"))
        self.assertEqual("judge", module.infer_prompt_task_type({}, target_role="judge"))

    def test_doubao_slotfiller_gets_host_directives_without_function_call_runtime_rules(self):
        module = load_module(self)
        config = {"models": {"slotfiller": {
            "provider": "doubao", "modelFamily": "doubao",
            "model": "doubao-pro", "reasoning": False, "promptProfile": "auto",
        }}}
        profile = module.resolve_profile(config, role="slotfiller")["profile"]
        recommendation = module.build_recommendation(
            profile, role="slotfiller", task_type="slot-filling")
        directive_ids = {item["id"] for item in recommendation["hostOptimizationDirectives"]}
        runtime_ids = {item["id"] for item in recommendation["runtimeRequirements"]}
        self.assertEqual("host-agent", recommendation["consumer"])
        self.assertIn("TASK-SLOT-01", directive_ids)
        self.assertIn("DOUBAO-SLOT-01", directive_ids)
        self.assertNotIn("DOUBAO-FC-RUNTIME-01", runtime_ids)
        self.assertNotIn("DOUBAO-FC-RUNTIME-02", runtime_ids)

    def test_runtime_requirements_are_filtered_by_role_and_task_type(self):
        module = load_module(self)
        config = {"models": {"candidate": {
            "provider": "qwen", "modelFamily": "qwen3", "model": "qwen3",
            "reasoning": False, "promptProfile": "auto",
        }}}
        profile = module.resolve_profile(config)["profile"]
        classification = module.build_recommendation(
            profile, role="candidate", task_type="classification")
        runtime_ids = {item["id"] for item in classification["runtimeRequirements"]}
        self.assertIn("QWEN-RUNTIME-01", runtime_ids)
        self.assertNotIn("QWEN-RUNTIME-02", runtime_ids)


if __name__ == "__main__":
    unittest.main()
