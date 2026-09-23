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

import runner


def native_call(name, arguments=None, call_id="call-1"):
    return {
        "content": "", "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)},
        }],
    }


class RunnerContractTest(unittest.TestCase):
    def test_main_writes_per_trajectory_info_and_appendable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "run")
            out = os.path.join(run, "trajectories-baseline")
            config_path = os.path.join(tmp, "config.json")
            task_path = os.path.join(tmp, "task.json")
            prompt_path = os.path.join(run, "prompt_v1.md")
            cases_path = os.path.join(tmp, "cases.json")
            os.makedirs(run)
            for path, value in (
                (config_path, {"models": {"candidate": {"model": "demo", "temperature": 0.2}}}),
                (task_path, {"initialPrompt": "fallback", "simulateMode": "off"}),
                (cases_path, [{"id": "case-1", "chat": {"simulateMode": "off", "messages": [{"role": "user", "content": "hi"}]}}]),
            ):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(value, f)
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write("system")
            argv = ["runner.py", "--config", config_path, "--task", task_path,
                    "--prompt", prompt_path, "--cases", cases_path, "--out", out,
                    "--run-id", "baseline", "--run-kind", "full"]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(runner, "run_all", return_value=[]):
                runner.main()
            self.assertTrue(os.path.isfile(os.path.join(out, "_run_info.json")))
            self.assertFalse(os.path.exists(os.path.join(run, "_run_info.json")))
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        row = manifest["invocations"][0]
        self.assertEqual("baseline", row["id"])
        self.assertEqual("full", row["kind"])
        self.assertEqual("trajectories-baseline", row["trajectoryDir"])
        self.assertEqual(64, len(row["prompt"]["sha256"]))
        self.assertEqual(1, row["cases"]["count"])

    def test_scripted_exact_never_rewrites_scripted_facts(self):
        case = {"id": "exact", "chat": {"simulateMode": "scripted", "scriptedExact": True,
                "messages": [{"role": "user", "content": "两个人"},
                             {"role": "user", "content": "明天18点"}]}}
        task = {"simulateMode": "scripted", "maxTurns": 3}
        replies = iter([{"content": "几点？", "tool_calls": []},
                        {"content": "收到", "tool_calls": []}])
        with mock.patch.object(runner.llm, "chat", side_effect=lambda *a, **k: next(replies)), \
                mock.patch.object(runner, "simulate_user_turn", side_effect=AssertionError("不应调用模拟用户")):
            result = runner.run_chat_case({}, case, task, "system", [], 10)
        users = [m["content"] for m in result["messages"] if m.get("role") == "user"]
        self.assertEqual(["两个人", "明天18点"], users)

    def test_unexpected_known_tool_is_model_error_not_device_error(self):
        tools = [
            {"type": "function", "function": {"name": "expected", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "extra", "parameters": {"type": "object"}}},
        ]
        case = {"id": "unexpected", "toolsExpect": ["expected"],
                "toolMock": {"expected": {"success": True}},
                "chat": {"simulateMode": "off", "messages": [{"role": "user", "content": "开始"}]}}
        task = {"toolMode": "native", "toolMockPolicy": "required", "maxTurns": 3,
                "toolMock": {"extra": {"success": True, "msg": "不应执行"}}}
        replies = iter([native_call("extra"), {"content": "结束", "tool_calls": []}])
        with mock.patch.object(runner.llm, "chat", side_effect=lambda *a, **k: next(replies)):
            result = runner.run_chat_case({}, case, task, "system", tools, 10)
        self.assertNotEqual("device_error", result["endReason"])
        self.assertFalse(result.get("deviceErrors"))
        tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
        self.assertIn("unexpected_tool_call", tool_messages[0]["content"])

    def test_terminal_tools_are_task_configured(self):
        tools = [{"type": "function", "function": {"name": "handoff", "parameters": {"type": "object"}}}]
        case = {"id": "terminal", "toolsExpect": ["handoff"],
                "toolMock": {"handoff": {"success": True}},
                "chat": {"simulateMode": "guided", "messages": [{"role": "user", "content": "转交"}]}}
        task = {"toolMode": "native", "terminalTools": ["handoff"], "maxTurns": 4}
        replies = iter([native_call("handoff"), {"content": "已转交", "tool_calls": []}])
        with mock.patch.object(runner.llm, "chat", side_effect=lambda *a, **k: next(replies)), \
                mock.patch.object(runner, "simulate_user_turn", side_effect=AssertionError("终态后不应继续模拟")):
            result = runner.run_chat_case({}, case, task, "system", tools, 10)
        self.assertEqual("completed", result["endReason"])
        self.assertEqual(2, result["turns"])

    def test_deepseek_thinking_replays_reasoning_content_after_tool_call(self):
        tools = [{"type": "function", "function": {
            "name": "query_status", "parameters": {"type": "object"}
        }}]
        case = {
            "id": "reasoning-replay", "toolsExpect": ["query_status"],
            "toolMock": {"query_status": {"success": True, "status": "processing"}},
            "chat": {"simulateMode": "off", "messages": [
                {"role": "user", "content": "查询状态"}
            ]},
        }
        task = {"toolMode": "native", "toolMockPolicy": "required", "maxTurns": 3,
                "terminalTools": ["query_status"]}
        config = {"models": {"candidate": {
            "provider": "deepseek", "model": "deepseek-reasoner", "reasoning": True,
        }}}
        captured = {}

        def fake_chat(_config, _role, messages, **kwargs):
            if "messages" not in captured:
                captured["messages"] = None
                reply = native_call("query_status")
                reply["reasoning_content"] = "先确认需要查询状态"
                return reply
            captured["messages"] = messages
            return {"content": "状态处理中", "tool_calls": []}

        with mock.patch.object(runner.llm, "chat", side_effect=fake_chat):
            result = runner.run_chat_case(config, case, task, "system", tools, 10)
        assistant = next(item for item in captured["messages"]
                         if item.get("role") == "assistant" and item.get("tool_calls"))
        self.assertEqual("先确认需要查询状态", assistant["reasoning_content"])
        self.assertEqual("先确认需要查询状态", result["messages"][1]["reasoning"])

    def test_selftest_selection_covers_distinct_contract_risks(self):
        task = {"terminalTools": ["handoff"]}
        cases = [
            {"id": "plain", "toolsExpect": [], "chat": {"messages": [{"role": "user", "content": "hi"}]}},
            {"id": "multi", "toolsExpect": ["query", "query"], "expectedToolArgs": {"query": [{}, {}]},
             "toolMock": {"query": [{"success": True}, {"success": True}]}},
            {"id": "failure", "toolsExpect": ["query"], "toolMock": {"query": {"success": False}}},
            {"id": "terminal", "toolsExpect": ["handoff"], "toolMock": {"handoff": {"success": True}}},
            {"id": "sequence", "toolsExpect": ["query"], "toolMock": {"query": [{"success": True}]}},
        ]
        selected = runner.select_selftest_cases(cases, task, 5)
        self.assertEqual({c["id"] for c in cases}, {c["id"] for c in selected})


if __name__ == "__main__":
    unittest.main()
