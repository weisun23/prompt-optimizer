# -*- coding: utf-8 -*-
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

try:
    import iteration_guard
except ImportError:
    iteration_guard = None


class IterationGuardTest(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(iteration_guard, "缺少单根因迭代门禁")

    def valid_plan(self, change_mode="local"):
        return {
            "schemaVersion": 3, "workflowId": "evidence-driven-single-hypothesis",
            "rootCauseHypothesis": "错误来自最后业务节点识别边界",
            "changeMode": change_mode, "architectureTrack": "production-candidate",
            "changeScope": ["终态判断规则"],
            "requirementIntegration": {
                "newRequirement": "修正最后业务节点的识别边界",
                "searchedLocations": ["终态判断规则", "业务完成定义"],
                "existingRuleLocations": ["终态判断规则"],
                "sharedDecisionAxis": "最后一个真实业务节点是否已经解决",
                "strategy": "merge-existing",
                "targetLocations": ["终态判断规则"],
                "rulesReplacedOrRemoved": [],
                "newRuleJustification": "",
                "expectedStructureEffect": "合并到现有终态判断，不新增章节",
            },
            "problemCaseIds": ["fail-1", "fail-2"],
            "neighborPassCaseIds": ["pass-1", "pass-2"],
            "boundaryPairs": [{"problemCaseId": "fail-1",
                               "neighborPassCaseIds": ["pass-1"],
                               "structuralDifference": "最后业务节点是否已经解决"}],
            "repeatRuns": 3, "unstableRepeatRuns": 5,
            "acceptancePolicy": {"schemaVersion": 1, "mode": "deterministic",
                                 "source": "user-confirmed",
                                 "successCriteria": "问题集、关键集与保护集全部通过",
                                 "criticalCaseIds": [], "protectedCaseIds": ["pass-1", "pass-2"]},
        }

    def test_valid_plan_requires_one_hypothesis_problem_and_neighbor_sets(self):
        self.require_module()
        self.assertEqual([], iteration_guard.validate_plan(self.valid_plan()))
        broken = self.valid_plan()
        broken.update({"rootCauseHypothesis": "", "changeScope": [], "problemCaseIds": [],
                       "neighborPassCaseIds": [], "repeatRuns": 1})
        errors = "\n".join(iteration_guard.validate_plan(broken))
        for term in ("rootCauseHypothesis", "changeScope", "problemCaseIds",
                     "neighborPassCaseIds", "repeatRuns"):
            self.assertIn(term, errors)

    def test_boundary_pairs_and_short_repeat_runs_require_explicit_evidence(self):
        self.require_module()
        plan = self.valid_plan()
        plan["boundaryPairs"] = []
        self.assertIn("boundaryPairs", "\n".join(iteration_guard.validate_plan(plan)))
        plan = self.valid_plan()
        plan["repeatRuns"] = 2
        errors = "\n".join(iteration_guard.validate_plan(plan))
        self.assertIn("repeatRunsJustification", errors)
        plan["repeatRunsJustification"] = "候选模型输出完全确定且历史三轮无波动"
        self.assertEqual([], iteration_guard.validate_plan(plan))

    def test_whole_rewrite_requires_structural_justification(self):
        self.require_module()
        plan = self.valid_plan(change_mode="whole-rewrite")
        self.assertTrue(any("rewriteJustification" in item for item in iteration_guard.validate_plan(plan)))
        plan["rewriteJustification"] = "多个章节存在相互矛盾的权威状态定义"
        plan["requirementIntegration"].update({
            "strategy": "structural-refactor",
            "targetLocations": ["终态判断规则"],
            "expectedStructureEffect": "重构互相冲突的权威状态定义",
        })
        self.assertEqual([], iteration_guard.validate_plan(plan))

    def test_requirement_integration_is_mandatory_before_editing(self):
        plan = self.valid_plan()
        plan.pop("requirementIntegration")
        self.assertIn("requirementIntegration", "\n".join(
            iteration_guard.validate_plan(plan)))

    def test_new_local_rule_requires_search_evidence_and_justification(self):
        plan = self.valid_plan()
        integration = plan["requirementIntegration"]
        integration.update({"strategy": "new-local-rule", "searchedLocations": [],
                            "existingRuleLocations": [], "newRuleJustification": ""})
        errors = "\n".join(iteration_guard.validate_plan(plan))
        self.assertIn("searchedLocations", errors)
        self.assertIn("newRuleJustification", errors)

    def test_generalize_replace_requires_removed_rules_and_targets_within_scope(self):
        plan = self.valid_plan()
        integration = plan["requirementIntegration"]
        integration.update({"strategy": "generalize-replace",
                            "targetLocations": ["未授权章节"],
                            "rulesReplacedOrRemoved": []})
        errors = "\n".join(iteration_guard.validate_plan(plan))
        self.assertIn("rulesReplacedOrRemoved", errors)
        self.assertIn("changeScope", errors)

    def test_acceptance_requires_repeated_stability_neighbors_and_full_regression(self):
        self.require_module()
        plan = self.valid_plan()
        valid = {"problemRuns": [{"failedCaseIds": []}] * 3,
                 "neighborRuns": [{"regressedCaseIds": []}] * 3,
                 "criticalFailures": [], "goalMet": True, "unacceptableRegressions": [],
                 "stableAcrossRuns": True,
                 "fullRegression": {"completed": True, "unacceptableRegression": False}}
        self.assertEqual([], iteration_guard.validate_acceptance(plan, valid))
        broken = dict(valid)
        broken.update({"problemRuns": [{"failedCaseIds": []}],
                       "neighborRuns": [{"regressedCaseIds": ["pass-1"]}, {"regressedCaseIds": []}],
                       "stableAcrossRuns": False,
                       "fullRegression": {"completed": False, "unacceptableRegression": True}})
        errors = "\n".join(iteration_guard.validate_acceptance(plan, broken))
        for term in ("重复", "相邻正确集", "稳定", "全量回归"):
            self.assertIn(term, errors)

    def test_initial_instability_requires_five_runs_before_acceptance(self):
        self.require_module()
        plan = self.valid_plan()
        result = {"problemRuns": [{"failedCaseIds": []}] * 3,
                  "neighborRuns": [{"regressedCaseIds": []}] * 3,
                  "criticalFailures": [], "goalMet": True, "unacceptableRegressions": [],
                  "hadInitialInstability": True, "stableAcrossRuns": True,
                  "fullRegression": {"completed": True, "unacceptableRegression": False}}
        self.assertIn("5", "\n".join(iteration_guard.validate_acceptance(plan, result)))
        result["problemRuns"] = [{"failedCaseIds": []}] * 5
        result["neighborRuns"] = [{"regressedCaseIds": []}] * 5
        self.assertEqual([], iteration_guard.validate_acceptance(plan, result))

    def test_experimental_architecture_can_be_kept_but_not_accepted(self):
        self.require_module()
        plan = self.valid_plan(change_mode="whole-rewrite")
        plan["architectureTrack"] = "experimental"
        plan["rewriteJustification"] = "多个权威规则互相冲突，需要统一分区架构"
        plan["requirementIntegration"].update({
            "strategy": "structural-refactor",
            "expectedStructureEffect": "重构互相冲突的权威状态定义",
        })
        self.assertEqual([], iteration_guard.validate_plan(plan))
        result = {"problemRuns": [{"failedCaseIds": []}] * 3,
                  "neighborRuns": [{"regressedCaseIds": []}] * 3,
                  "criticalFailures": [], "goalMet": True, "unacceptableRegressions": [],
                  "hadInitialInstability": False, "stableAcrossRuns": True,
                  "fullRegression": {"completed": True, "unacceptableRegression": False}}
        self.assertIn("experimental", "\n".join(iteration_guard.validate_acceptance(plan, result)))


if __name__ == "__main__":
    unittest.main()
