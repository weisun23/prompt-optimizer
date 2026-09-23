# -*- coding: utf-8 -*-
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import case_taxonomy
import workflow_policy
import select_cases
import failure_triage


def taxonomy():
    return {
        "schemaVersion": 1,
        "source": "用户确认的业务契约",
        "dimensions": [
            {"id": "business_stage", "name": "业务阶段", "purpose": "组织专项覆盖",
             "required": True, "values": [
                 {"id": "collecting", "name": "信息收集", "assignmentRule": "执行条件尚未满足"},
                 {"id": "executing", "name": "执行阶段", "assignmentRule": "执行条件已经满足"},
             ]},
            {"id": "impact", "name": "失败影响", "purpose": "任务自定义优先级",
             "required": False, "values": [
                 {"id": "p0", "name": "本任务一级影响", "assignmentRule": "由业务契约定义"},
                 {"id": "p1", "name": "本任务二级影响", "assignmentRule": "由业务契约定义"},
             ]},
        ],
        "stabilityPolicy": {
            "source": "用户确认的重复运行口径",
            "labels": [
                {"id": "always", "name": "本任务必现", "assignmentRule": "所有有效复测均失败"},
                {"id": "variable", "name": "本任务波动", "assignmentRule": "有效复测结果不一致"},
                {"id": "not_retested", "name": "尚未复测", "assignmentRule": "没有复测证据"},
            ],
        },
    }


class CaseTaxonomyTest(unittest.TestCase):
    def test_taxonomy_accepts_task_defined_dimensions_and_levels(self):
        self.assertEqual([], case_taxonomy.validate_taxonomy(taxonomy()))
        custom = taxonomy()
        custom["dimensions"][1]["values"] = [
            {"id": "legal_blocker", "name": "法规阻断", "assignmentRule": "触发法规禁止项"}
        ]
        self.assertEqual([], case_taxonomy.validate_taxonomy(custom))

    def test_taxonomy_values_must_match_required_task_dimensions(self):
        errors = "\n".join(case_taxonomy.validate_values(
            taxonomy(), {"business_stage": "missing", "unknown": "x"}))
        self.assertIn("business_stage", errors)
        self.assertIn("unknown", errors)
        self.assertIn("collecting", "\n".join(case_taxonomy.validate_values(
            taxonomy(), {})))

    def test_coverage_plan_uses_taxonomy_values_instead_of_fixed_risk_enum(self):
        plan = {
            "schemaVersion": 2,
            "taxonomy": {"path": "case-taxonomy.json", "sha256": "a" * 64},
            "coverageItems": [{
                "id": "stage-collecting", "description": "信息收集正反边界",
                "taxonomyValues": {"business_stage": "collecting", "impact": "p0"},
                "positiveCaseIds": [], "negativeCaseIds": [], "status": "pending",
            }],
            "resourceBudget": None,
        }
        self.assertEqual([], workflow_policy.validate_coverage_plan(plan, taxonomy()))
        plan["coverageItems"][0]["risk"] = "critical"
        self.assertIn("risk", "\n".join(
            workflow_policy.validate_coverage_plan(plan, taxonomy())))

    def test_case_selection_filters_dynamic_taxonomy_dimensions(self):
        cases = [
            {"id": "a", "taxonomyValues": {"business_stage": "collecting", "impact": "p0"}},
            {"id": "b", "taxonomyValues": {"business_stage": "executing", "impact": "p0"}},
            {"id": "c", "taxonomyValues": {"business_stage": "collecting", "impact": "p1"}},
        ]
        selected = select_cases.select_by_taxonomy(
            cases, {"business_stage": "collecting", "impact": ["p0", "p1"]})
        self.assertEqual(["a", "c"], [row["id"] for row in selected])

    def test_failure_triage_uses_dynamic_taxonomy_and_raw_stability_evidence(self):
        triage = {"schemaVersion": 3, "taxonomy": taxonomy(), "items": [{
            "caseId": "a", "caseStatus": "active", "failureOwner": "model_failure",
            "confidence": "high", "taxonomyValues": {
                "business_stage": "executing", "impact": "p0"},
            "stabilityEvidence": {"validRuns": 3, "failureRuns": 3,
                                  "passRuns": 0, "invalidRuns": 0,
                                  "environmentSha256": "b" * 64},
            "stabilityLabel": "always", "optimizationEligible": True,
            "evidence": ["三次同环境运行均失败"], "authoritativeBasis": ["正式契约"],
            "contextScope": "full", "neighborCaseIds": ["pass-a"],
            "decisionAnchor": "not-applicable：单轮任务", "fieldsUsed": [],
            "fieldProvenance": [], "action": "进入专项问题集",
        }]}
        self.assertEqual([], failure_triage.validate_triage(triage))
        triage["items"][0]["failureOwner"] = "device_error"
        self.assertIn("optimizationEligible", "\n".join(
            failure_triage.validate_triage(triage)))


if __name__ == "__main__":
    unittest.main()
