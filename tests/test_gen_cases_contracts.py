# -*- coding: utf-8 -*-
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import gen_cases


def tool(name, properties=None):
    return {"type": "function", "function": {
        "name": name,
        "parameters": {"type": "object", "properties": properties or {}},
    }}


def base_case(**updates):
    case = {
        "id": "case-1", "type": "chat",
        "chat": {"simulateMode": "scripted", "scriptedExact": True,
                 "messages": [{"role": "user", "content": "时间是18:00"}]},
        "toolsExpect": ["query"],
        "expectedToolArgs": {"query": {"time": "18:00"}},
        "toolMock": {"query": {"success": True}},
        "modelVisibleEvidence": [{"fact": "查询时间", "source": "user-message", "value": "18:00"}],
        "expectedCallPolicy": {"mode": "immediate", "allowedClarifications": 0,
                               "maxAdditionalTurns": 0},
    }
    case.update(updates)
    return case


def taxonomy():
    return {"schemaVersion": 1, "source": "user-confirmed", "dimensions": [{
        "id": "scenario", "name": "场景", "purpose": "专项分类", "required": True,
        "values": [{"id": "normal", "name": "常规", "assignmentRule": "常规流程"}],
    }], "stabilityPolicy": {"source": "user-confirmed", "labels": [
        {"id": "not_retested", "name": "尚未复测", "assignmentRule": "没有复测证据"},
    ]}}


class GenCaseContractTest(unittest.TestCase):
    def task(self):
        return {"tools": [tool("query", {"time": {"type": "string"}}), tool("handoff")],
                "toolMockPolicy": "required", "terminalTools": ["handoff"]}

    def test_repeated_tool_requires_matching_argument_group_count(self):
        case = base_case(toolsExpect=["query", "query"],
                         expectedToolArgs={"query": {"time": "18:00"}},
                         toolMock={"query": [{"success": True}, {"success": True}]})
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("参数组数量", dropped[0][1])

    def test_argument_group_without_tools_expect_is_rejected(self):
        case = base_case(toolsExpect=[], expectedToolArgs={"query": {"time": "18:00"}})
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("无对应 toolsExpect", dropped[0][1])

    def test_sequence_mock_must_cover_expected_call_count(self):
        case = base_case(toolsExpect=["query", "query"],
                         expectedToolArgs={"query": [{"time": "18:00"}, {"time": "18:00"}]},
                         toolMock={"query": [{"success": True}]})
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("Mock 数量不足", dropped[0][1])

    def test_terminal_tool_must_be_last(self):
        case = base_case(toolsExpect=["handoff", "query"],
                         expectedToolArgs={"handoff": {}, "query": {"time": "18:00"}},
                         toolMock={"handoff": {"success": True}, "query": {"success": True}})
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("终态工具必须位于最后", dropped[0][1])

    def test_script_missing_expected_fact_is_warned(self):
        case = base_case(expectedToolArgs={"query": {"time": "19:30"}})
        valid, dropped, warnings = gen_cases.validate([case], self.task())
        self.assertEqual(1, len(valid))
        self.assertFalse(dropped)
        self.assertIn("脚本未发现期望参数事实", warnings[0][1])

    def test_scenario_metadata_cannot_replace_model_visible_evidence(self):
        case = base_case(scenario="历史中已经满位", modelVisibleEvidence=[{
            "fact": "已经满位", "source": "user-message", "value": "满位",
        }])
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("模型可见", dropped[0][1])

    def test_tool_case_requires_explicit_call_timing_policy(self):
        case = base_case()
        case.pop("expectedCallPolicy")
        valid, dropped, _ = gen_cases.validate([case], self.task())
        self.assertFalse(valid)
        self.assertIn("expectedCallPolicy", dropped[0][1])

    def test_expected_output_must_name_authoritative_source_when_precedence_is_configured(self):
        task = self.task()
        task["evaluationSourcePrecedence"] = ["prompt-embedded-label", "businessLabel"]
        case = base_case(expectedOutput={"label": "A"})
        valid, dropped, _ = gen_cases.validate([case], task)
        self.assertFalse(valid)
        self.assertIn("expectedOutputSource", dropped[0][1])
        case["expectedOutputSource"] = "businessLabel"
        case["expectedOutputSourceEvidence"] = "任务契约明确在无嵌入标签时读取 businessLabel"
        valid, dropped, _ = gen_cases.validate([case], task)
        self.assertEqual(1, len(valid))
        self.assertFalse(dropped)

    def test_generation_plan_must_cover_every_pending_contract_item(self):
        coverage = {"schemaVersion": 2,
                    "taxonomy": {"path": "case-taxonomy.json", "sha256": "a" * 64},
                    "coverageItems": [
            {"id": "a", "description": "A边界", "taxonomyValues": {"scenario": "normal"},
             "positiveCaseIds": [], "negativeCaseIds": [], "status": "pending"},
            {"id": "b", "description": "B边界", "taxonomyValues": {"scenario": "normal"},
             "positiveCaseIds": [], "negativeCaseIds": [], "status": "pending"},
        ]}
        generation = [{"category": "A", "coverageItemIds": ["a"],
                       "taxonomyValues": {"scenario": "normal"},
                       "count": 2, "pilotCount": 1}]
        errors = "\n".join(gen_cases.validate_generation_plan(generation, coverage, taxonomy()))
        self.assertIn("b", errors)
        generation[0]["coverageItemIds"].append("b")
        self.assertEqual([], gen_cases.validate_generation_plan(generation, coverage, taxonomy()))


if __name__ == "__main__":
    unittest.main()
