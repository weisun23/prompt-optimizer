# -*- coding: utf-8 -*-
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import llm


TOOLS = [{"type": "function", "function": {
    "name": "lock_table", "parameters": {"type": "object"}
}}]


class LlmProfileTest(unittest.TestCase):
    def test_missing_non_slotfiller_role_does_not_reuse_candidate(self):
        config = {"models": {"candidate": {"model": "qwen", "base_url": "http://local"}}}
        with self.assertRaisesRegex(ValueError, "models.judge"):
            llm.role_model_config(config, "judge")

    def test_slotfiller_explicitly_reuses_candidate(self):
        config = {"models": {"candidate": {
            "model": "qwen", "base_url": "http://local", "provider": "qwen"
        }}}
        result = llm.role_model_config(config, "slotfiller")
        self.assertEqual("qwen", result["model"])
        self.assertEqual("qwen", result["provider"])
        self.assertEqual("candidate", result["reusedFrom"])

    def test_tool_choice_supports_auto_none_required_and_named(self):
        self.assertEqual("auto", llm.resolve_tool_choice("auto", TOOLS))
        self.assertEqual("none", llm.resolve_tool_choice("none", TOOLS))
        self.assertEqual("required", llm.resolve_tool_choice("required", TOOLS))
        self.assertEqual(
            {"type": "function", "function": {"name": "lock_table"}},
            llm.resolve_tool_choice("named:lock_table", TOOLS))

    def test_named_unknown_tool_and_required_without_tools_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "不在 tools"):
            llm.resolve_tool_choice("named:missing", TOOLS)
        with self.assertRaisesRegex(ValueError, "没有提供 tools"):
            llm.resolve_tool_choice("required", [])
        with self.assertRaisesRegex(ValueError, "不支持"):
            llm.resolve_tool_choice("sometimes", TOOLS)

    def test_chat_uses_role_tool_choice_policy(self):
        config = {"models": {"candidate": {
            "model": "qwen", "base_url": "http://local", "auth_type": "none",
            "toolChoicePolicy": "named:lock_table",
        }}}
        captured = {}
        original = llm.call_model

        def fake_call(url, headers, body, timeout, max_retries, no_proxy=False):
            captured.update(body)
            return {"choices": [{"message": {"content": "", "tool_calls": []},
                                  "finish_reason": "stop"}]}

        llm.call_model = fake_call
        try:
            llm.chat(config, "candidate", [{"role": "user", "content": "开始"}], tools=TOOLS)
        finally:
            llm.call_model = original
        self.assertEqual(
            {"type": "function", "function": {"name": "lock_table"}},
            captured["tool_choice"])

    def test_reasoning_replay_is_enabled_only_by_capability_or_deepseek_thinking(self):
        self.assertTrue(llm.reasoning_replay_enabled({"models": {"candidate": {
            "provider": "deepseek", "reasoning": True,
        }}}, "candidate"))
        self.assertTrue(llm.reasoning_replay_enabled({"models": {"candidate": {
            "provider": "qwen", "reasoning": False,
            "capabilities": {"reasoningReplay": True},
        }}}, "candidate"))
        self.assertFalse(llm.reasoning_replay_enabled({"models": {"candidate": {
            "provider": "deepseek", "reasoning": False,
        }}}, "candidate"))


if __name__ == "__main__":
    unittest.main()
