# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import workflow_policy


class WorkflowPolicyTest(unittest.TestCase):
    def taxonomy(self):
        return {"schemaVersion": 1, "source": "user-confirmed", "dimensions": [{
            "id": "label", "name": "标签", "purpose": "覆盖标签边界", "required": True,
            "values": [{"id": "a", "name": "A", "assignmentRule": "属于A"},
                       {"id": "b", "name": "B", "assignmentRule": "属于B"}],
        }], "stabilityPolicy": {"source": "user-confirmed", "labels": [
            {"id": "not_retested", "name": "尚未复测", "assignmentRule": "无复测"},
        ]}}

    def deterministic_policy(self):
        return {
            "schemaVersion": 1,
            "mode": "deterministic",
            "source": "user-confirmed",
            "successCriteria": "问题集、关键集和保护集全部通过",
            "criticalCaseIds": ["critical-1"],
            "protectedCaseIds": ["neighbor-1"],
        }

    def test_coverage_is_driven_by_contract_items_not_case_limit(self):
        plan = {
            "schemaVersion": 2,
            "taxonomy": {"path": "case-taxonomy.json", "sha256": "a" * 64},
            "coverageItems": [
                {"id": "label-a", "description": "标签A边界", "taxonomyValues": {"label": "a"},
                 "positiveCaseIds": ["a-positive"], "negativeCaseIds": ["a-negative"],
                 "status": "covered"},
                {"id": "label-b", "description": "标签B边界", "taxonomyValues": {"label": "b"},
                 "positiveCaseIds": [], "negativeCaseIds": [], "status": "pending"},
            ],
        }
        self.assertEqual([], workflow_policy.validate_coverage_plan(plan, self.taxonomy()))
        self.assertEqual(["label-b"], workflow_policy.pending_coverage_ids(plan))
        self.assertFalse(workflow_policy.coverage_complete(plan))
        plan["coverageItems"][1].update({
            "positiveCaseIds": ["b-positive"], "negativeCaseIds": ["b-negative"],
            "status": "covered",
        })
        self.assertTrue(workflow_policy.coverage_complete(plan, self.taxonomy()))

    def test_deterministic_acceptance_uses_confirmed_critical_cases_not_a_global_rate(self):
        policy = self.deterministic_policy()
        rejected = {
            "problemRuns": [{"failedCaseIds": ["critical-1", "problem-5"]}],
            "neighborRuns": [{"regressedCaseIds": []}],
            "criticalFailures": [], "goalMet": True, "unacceptableRegressions": [],
            "stableAcrossRuns": True,
            "fullRegression": {"completed": True, "unacceptableRegression": False},
        }
        self.assertIn("critical-1", "\n".join(
            workflow_policy.evaluate_acceptance(policy, rejected)))
        accepted = dict(rejected)
        accepted["problemRuns"] = [{"failedCaseIds": ["problem-5"]}]
        self.assertEqual([], workflow_policy.evaluate_acceptance(policy, accepted))

    def test_stochastic_acceptance_uses_user_goal_and_severity_not_global_rate(self):
        policy = {
            "schemaVersion": 1, "mode": "stochastic", "source": "user-confirmed",
            "successCriteria": "相对基线稳定提升且无高严重度退化",
            "criticalCaseIds": [], "protectedCaseIds": ["boundary-1"],
        }
        result = {
            "goalMet": True, "stableAcrossRuns": True,
            "criticalFailures": [], "unacceptableRegressions": [],
            "fullRegression": {"completed": True, "unacceptableRegression": False},
        }
        self.assertEqual([], workflow_policy.evaluate_acceptance(policy, result))
        result["goalMet"] = False
        self.assertIn("用户确认的成功标准", "\n".join(
            workflow_policy.evaluate_acceptance(policy, result)))

    def test_rejected_candidate_continues_and_reports_only_on_success_or_request(self):
        state = {
            "status": "CANDIDATE_REJECTED_CONTINUE",
            "actionableRootCauses": ["next-root"],
            "untestedHypotheses": [], "newEvidenceAvailable": False,
        }
        self.assertTrue(workflow_policy.should_continue(state))
        self.assertFalse(workflow_policy.should_generate_report(state["status"], False))
        self.assertTrue(workflow_policy.should_generate_report("ACCEPTED", False))
        self.assertTrue(workflow_policy.should_generate_report("IN_PROGRESS", True))
        exhausted = dict(state, actionableRootCauses=[])
        self.assertFalse(workflow_policy.should_continue(exhausted))

    def test_example_config_has_no_arbitrary_round_or_case_caps(self):
        with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as stream:
            config = json.load(stream)
        budget = config["budget"]
        for key in ("maxRounds", "maxCasesPerRun", "casesPerCategory", "pilotPerCategory"):
            self.assertNotIn(key, budget)


if __name__ == "__main__":
    unittest.main()
