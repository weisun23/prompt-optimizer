#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证单根因、最小改动、重复问题集和全量回归证据。"""

import argparse
import json

import workflow_policy


WORKFLOW_ID = "evidence-driven-single-hypothesis"
VALID_CHANGE_MODES = ("local", "whole-rewrite")
VALID_ARCHITECTURE_TRACKS = ("production-candidate", "experimental")
VALID_INTEGRATION_STRATEGIES = (
    "merge-existing", "generalize-replace", "new-local-rule", "structural-refactor",
)


def _nonempty_list(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))


def _text_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def validate_requirement_integration(plan):
    """新增需求必须先分析合并位置，再决定合并、替换、新增或重构。"""
    integration = plan.get("requirementIntegration")
    if not isinstance(integration, dict):
        return ["requirementIntegration 必须为对象，修改前先完成需求整合分析"]
    errors = []
    for key in ("newRequirement", "sharedDecisionAxis", "expectedStructureEffect"):
        if not str(integration.get(key) or "").strip():
            errors.append("requirementIntegration.%s 不能为空" % key)
    for key in ("searchedLocations", "targetLocations"):
        if not _nonempty_list(integration.get(key)):
            errors.append("requirementIntegration.%s 不能为空" % key)
    for key in ("existingRuleLocations", "rulesReplacedOrRemoved"):
        if not _text_list(integration.get(key)):
            errors.append("requirementIntegration.%s 必须为字符串数组" % key)
    strategy = integration.get("strategy")
    if strategy not in VALID_INTEGRATION_STRATEGIES:
        errors.append("requirementIntegration.strategy 无效")
    existing = integration.get("existingRuleLocations") or []
    replaced = integration.get("rulesReplacedOrRemoved") or []
    if strategy in ("merge-existing", "generalize-replace") and not existing:
        errors.append("%s 必须列出 existingRuleLocations" % strategy)
    if strategy == "generalize-replace" and not replaced:
        errors.append("generalize-replace 必须列出 rulesReplacedOrRemoved")
    if strategy == "new-local-rule" and not str(integration.get("newRuleJustification") or "").strip():
        errors.append("new-local-rule 必须提供 newRuleJustification，说明为何无法合并")
    change_scope = set(plan.get("changeScope") or [])
    targets = set(integration.get("targetLocations") or [])
    outside = sorted(targets - change_scope)
    if outside:
        errors.append("requirementIntegration.targetLocations 不在 changeScope：%s" % "、".join(outside))
    change_mode = plan.get("changeMode")
    if strategy == "structural-refactor" and change_mode != "whole-rewrite":
        errors.append("structural-refactor 必须使用 changeMode=whole-rewrite")
    if change_mode == "whole-rewrite" and strategy != "structural-refactor":
        errors.append("whole-rewrite 必须使用 requirementIntegration.strategy=structural-refactor")
    return errors


def validate_plan(plan):
    errors = []
    if not isinstance(plan, dict):
        return ["迭代计划必须为对象"]
    if plan.get("schemaVersion") != 3:
        return ["迭代计划 schemaVersion 必须为 3；最新版要求需求整合分析"]
    if plan.get("workflowId") != WORKFLOW_ID:
        errors.append("workflowId 必须为 %s" % WORKFLOW_ID)
    if not str(plan.get("rootCauseHypothesis") or "").strip():
        errors.append("rootCauseHypothesis 必须明确本轮唯一根因假设")
    if plan.get("changeMode") not in VALID_CHANGE_MODES:
        errors.append("changeMode 必须为 local 或 whole-rewrite")
    if plan.get("architectureTrack") not in VALID_ARCHITECTURE_TRACKS:
        errors.append("architectureTrack 必须为 production-candidate 或 experimental")
    if not _nonempty_list(plan.get("changeScope")):
        errors.append("changeScope 必须列出本轮允许修改的位置")
    errors.extend(validate_requirement_integration(plan))
    if not _nonempty_list(plan.get("problemCaseIds")):
        errors.append("problemCaseIds 不能为空")
    if not _nonempty_list(plan.get("neighborPassCaseIds")):
        errors.append("neighborPassCaseIds 不能为空，必须保护相邻正确集")
    repeat_runs = plan.get("repeatRuns")
    repeat_runs_valid = (not isinstance(repeat_runs, bool)
                         and isinstance(repeat_runs, int) and repeat_runs >= 2)
    if not repeat_runs_valid:
        errors.append("repeatRuns 至少为 2，用于排除随机波动")
    elif repeat_runs < 3 and not str(plan.get("repeatRunsJustification") or "").strip():
        errors.append("repeatRuns 低于推荐的 3 次时必须提供 repeatRunsJustification")
    unstable_runs = plan.get("unstableRepeatRuns")
    if (isinstance(unstable_runs, bool) or not isinstance(unstable_runs, int)
            or unstable_runs < 5 or (repeat_runs_valid and unstable_runs < repeat_runs)):
        errors.append("unstableRepeatRuns 至少为 5 且不得小于 repeatRuns")
    if _nonempty_list(plan.get("problemCaseIds")) and _nonempty_list(plan.get("neighborPassCaseIds")):
        overlap = sorted(set(plan["problemCaseIds"]) & set(plan["neighborPassCaseIds"]))
        if overlap:
            errors.append("问题集与相邻正确集不能重叠：%s" % "、".join(overlap))
    pairs = plan.get("boundaryPairs")
    if not isinstance(pairs, list) or not pairs:
        errors.append("boundaryPairs 不能为空，必须明确问题用例与相邻反例的结构差异")
        pairs = []
    problem_ids = set(plan.get("problemCaseIds") or []) if _nonempty_list(plan.get("problemCaseIds")) else set()
    neighbor_ids = (set(plan.get("neighborPassCaseIds") or [])
                    if _nonempty_list(plan.get("neighborPassCaseIds")) else set())
    for index, pair in enumerate(pairs, 1):
        if not isinstance(pair, dict):
            errors.append("boundaryPairs 第 %d 项必须为对象" % index)
            continue
        problem_id = pair.get("problemCaseId")
        paired_neighbors = pair.get("neighborPassCaseIds")
        if not isinstance(problem_id, str) or not problem_id.strip() or problem_id not in problem_ids:
            errors.append("boundaryPairs 第 %d 项的 problemCaseId 不在问题集" % index)
        if not _nonempty_list(paired_neighbors):
            errors.append("boundaryPairs 第 %d 项必须包含相邻正确用例" % index)
        elif not set(paired_neighbors).issubset(neighbor_ids):
            errors.append("boundaryPairs 第 %d 项包含不在相邻正确集的用例" % index)
        if not str(pair.get("structuralDifference") or "").strip():
            errors.append("boundaryPairs 第 %d 项缺少 structuralDifference" % index)
    if plan.get("changeMode") == "whole-rewrite" and not str(plan.get("rewriteJustification") or "").strip():
        errors.append("整体重写必须提供 rewriteJustification 说明结构性根因")
    policy = plan.get("acceptancePolicy")
    if not isinstance(policy, dict):
        errors.append("acceptancePolicy 必须为对象")
    else:
        errors.extend(workflow_policy.validate_acceptance_policy(policy))
    return errors


def validate_acceptance(plan, results):
    errors = validate_plan(plan)
    if errors:
        return errors
    if not isinstance(results, dict):
        return ["迭代结果格式无效，必须为对象"]
    had_initial_instability = results.get("hadInitialInstability", False)
    if had_initial_instability not in (True, False):
        errors.append("hadInitialInstability 必须为布尔值")
        had_initial_instability = True
    repeat_runs = plan["unstableRepeatRuns"] if had_initial_instability else plan["repeatRuns"]
    policy = plan["acceptancePolicy"]
    problem_runs = results.get("problemRuns") or []
    neighbor_runs = results.get("neighborRuns") or []
    if len(problem_runs) < repeat_runs:
        errors.append("问题集重复验证次数不足：%d/%d" % (len(problem_runs), repeat_runs))
    if len(neighbor_runs) < repeat_runs:
        errors.append("相邻正确集重复验证次数不足：%d/%d" % (len(neighbor_runs), repeat_runs))
    if policy.get("mode") == "deterministic":
        invalid_problem = [index for index, row in enumerate(problem_runs, 1)
                           if not isinstance(row, dict) or not isinstance(row.get("failedCaseIds"), list)]
        invalid_neighbor = [index for index, row in enumerate(neighbor_runs, 1)
                            if not isinstance(row, dict) or not isinstance(row.get("regressedCaseIds"), list)]
        if invalid_problem:
            errors.append("问题集 failedCaseIds 无效的轮次：%s" % "、".join(map(str, invalid_problem)))
        if invalid_neighbor:
            errors.append("相邻正确集 regressedCaseIds 无效的轮次：%s" % "、".join(map(str, invalid_neighbor)))
    errors.extend(workflow_policy.evaluate_acceptance(policy, results))
    if plan.get("architectureTrack") == "experimental":
        errors.append("experimental 架构只能留存迭代，不能通过生产接受门禁")
    return errors


def main():
    parser = argparse.ArgumentParser(description="验证证据驱动单根因迭代计划")
    parser.add_argument("--plan", required=True, help="optimization_iteration.json")
    parser.add_argument("--results", default=None, help="包含重复与全量结果的 JSON")
    args = parser.parse_args()
    with open(args.plan, encoding="utf-8") as stream:
        plan = json.load(stream)
    if args.results:
        with open(args.results, encoding="utf-8") as stream:
            errors = validate_acceptance(plan, json.load(stream))
    else:
        errors = validate_plan(plan)
    if errors:
        raise SystemExit("迭代门禁失败：%s" % "；".join(errors))
    print("迭代门禁通过")


if __name__ == "__main__":
    main()
