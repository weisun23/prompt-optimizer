#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖、接受、持续优化与报告触发的确定性策略。"""

import re

import case_taxonomy


FINAL_STATUSES = frozenset(("ACCEPTED", "USER_STOPPED", "BUDGET_EXHAUSTED", "BLOCKED"))


def _text_list(value, allow_empty=False):
    return (isinstance(value, list) and (allow_empty or bool(value))
            and all(isinstance(item, str) and item.strip() for item in value))


def validate_coverage_plan(plan, taxonomy=None):
    """校验覆盖计划；用例数量由覆盖项决定，不设置全局条数上限。"""
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 2:
        return ["coverage-plan.json schemaVersion 必须为 2"]
    errors = []
    taxonomy_ref = plan.get("taxonomy")
    if not isinstance(taxonomy_ref, dict):
        errors.append("coveragePlan.taxonomy 必须为对象")
    else:
        if not str(taxonomy_ref.get("path") or "").strip():
            errors.append("coveragePlan.taxonomy.path 不能为空")
        if not re.fullmatch(r"[0-9a-f]{64}", str(taxonomy_ref.get("sha256") or "")):
            errors.append("coveragePlan.taxonomy.sha256 必须为64位小写哈希")
    if taxonomy is not None:
        errors.extend(case_taxonomy.validate_taxonomy(taxonomy))
    items = plan.get("coverageItems")
    if not isinstance(items, list) or not items:
        return errors + ["coverageItems 不能为空"]
    seen = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            errors.append("coverageItems 第 %d 项必须为对象" % index)
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append("coverageItems 第 %d 项缺少 id" % index)
            continue
        if item_id in seen:
            errors.append("coverageItems ID 重复：%s" % item_id)
        seen.add(item_id)
        if not str(item.get("description") or "").strip():
            errors.append("覆盖项 %s 缺少 description" % item_id)
        if "risk" in item:
            errors.append("覆盖项 %s 禁止使用固定 risk 枚举，改用 taxonomyValues" % item_id)
        if taxonomy is not None:
            for error in case_taxonomy.validate_values(taxonomy, item.get("taxonomyValues")):
                errors.append("覆盖项 %s：%s" % (item_id, error))
        elif not isinstance(item.get("taxonomyValues"), dict):
            errors.append("覆盖项 %s taxonomyValues 必须为对象" % item_id)
        status = item.get("status")
        if status not in ("pending", "covered"):
            errors.append("覆盖项 %s status 必须为 pending 或 covered" % item_id)
        positives = item.get("positiveCaseIds")
        negatives = item.get("negativeCaseIds")
        if not _text_list(positives, allow_empty=status == "pending"):
            errors.append("覆盖项 %s positiveCaseIds 无效" % item_id)
        if not _text_list(negatives, allow_empty=status == "pending"):
            errors.append("覆盖项 %s negativeCaseIds 无效" % item_id)
    budget = plan.get("resourceBudget")
    if budget is not None and not isinstance(budget, dict):
        errors.append("resourceBudget 必须为对象；未设置时表示不以固定数量截断覆盖")
    return errors


def pending_coverage_ids(plan):
    """返回尚未覆盖的契约项。"""
    return [item.get("id") for item in (plan.get("coverageItems") or [])
            if isinstance(item, dict) and item.get("status") != "covered"]


def coverage_complete(plan, taxonomy=None):
    """只有所有覆盖项及正反边界均完成时才算用例充分。"""
    return not validate_coverage_plan(plan, taxonomy) and not pending_coverage_ids(plan)


def validate_acceptance_policy(policy):
    """接受标准必须来自任务或用户确认，禁止使用全局80%等固定阈值。"""
    if not isinstance(policy, dict) or policy.get("schemaVersion") != 1:
        return ["acceptancePolicy.schemaVersion 必须为 1"]
    errors = []
    if policy.get("mode") not in ("deterministic", "stochastic"):
        errors.append("acceptancePolicy.mode 必须为 deterministic 或 stochastic")
    if not str(policy.get("source") or "").strip():
        errors.append("acceptancePolicy.source 必须记录用户或正式契约来源")
    if not str(policy.get("successCriteria") or "").strip():
        errors.append("acceptancePolicy.successCriteria 不能为空")
    for key in ("criticalCaseIds", "protectedCaseIds"):
        if not _text_list(policy.get(key), allow_empty=True):
            errors.append("acceptancePolicy.%s 必须为用例 ID 数组" % key)
    return errors


def evaluate_acceptance(policy, results):
    """按任务级策略判断候选；返回空数组表示可进入生产接受。"""
    errors = validate_acceptance_policy(policy)
    if errors:
        return errors
    if not isinstance(results, dict):
        return ["候选结果必须为对象"]
    mode = policy["mode"]
    critical = results.get("criticalFailures")
    if not isinstance(critical, list):
        errors.append("criticalFailures 必须为数组")
        critical = []
    elif critical:
        errors.append("关键用例仍失败：%s" % "、".join(map(str, critical)))
    critical_ids = set(policy.get("criticalCaseIds") or [])
    protected_ids = set(policy.get("protectedCaseIds") or [])
    if mode == "deterministic":
        for index, row in enumerate(results.get("problemRuns") or [], 1):
            failed = row.get("failedCaseIds") if isinstance(row, dict) else None
            if not isinstance(failed, list):
                errors.append("problemRuns 第 %d 轮缺少 failedCaseIds" % index)
            else:
                required_failed = [case_id for case_id in failed if case_id in critical_ids]
                if required_failed:
                    errors.append("问题集第 %d 轮关键用例失败：%s" % (
                        index, "、".join(map(str, required_failed))))
        for index, row in enumerate(results.get("neighborRuns") or [], 1):
            regressed = row.get("regressedCaseIds") if isinstance(row, dict) else None
            if not isinstance(regressed, list):
                errors.append("neighborRuns 第 %d 轮缺少 regressedCaseIds" % index)
            else:
                required_regressions = [case_id for case_id in regressed if case_id in protected_ids]
                if required_regressions:
                    errors.append("保护集第 %d 轮关键用例退化：%s" % (
                        index, "、".join(map(str, required_regressions))))
    if results.get("goalMet") is not True:
        errors.append("未达到用户确认的成功标准")
    regressions = results.get("unacceptableRegressions")
    if not isinstance(regressions, list):
        errors.append("unacceptableRegressions 必须为数组")
    elif regressions:
        errors.append("存在用户定义的不可接受退化：%s" % "、".join(map(str, regressions)))
    if results.get("stableAcrossRuns") is not True:
        errors.append("重复运行方向不稳定")
    full = results.get("fullRegression")
    if not isinstance(full, dict) or full.get("completed") is not True:
        errors.append("同口径全量回归尚未完成")
    elif full.get("unacceptableRegression") is not False:
        errors.append("全量回归存在不可接受退化或未给出明确结论")
    return errors


def should_continue(state):
    """失败候选默认继续；只有成功、用户停止、预算耗尽或真正阻塞才结束。"""
    if not isinstance(state, dict) or state.get("status") in FINAL_STATUSES:
        return False
    if state.get("status") not in ("IN_PROGRESS", "CANDIDATE_REJECTED_CONTINUE"):
        return False
    return bool(state.get("actionableRootCauses") or state.get("untestedHypotheses")
                or state.get("newEvidenceAvailable"))


def should_generate_report(status, user_requested=False):
    """正式报告只在候选成功或用户明确要求时生成。"""
    return bool(user_requested or status == "ACCEPTED")


def validate_root_cause_analysis(analysis):
    """根因分析必须定位最早分歧，并通过单变量消融验证关键猜测。"""
    if not isinstance(analysis, dict) or analysis.get("schemaVersion") != 1:
        return ["root-cause-analysis.json schemaVersion 必须为 1"]
    errors = []
    if not str(analysis.get("rootCause") or "").strip():
        errors.append("rootCause 不能为空")
    divergence = analysis.get("earliestDivergence")
    if not isinstance(divergence, dict):
        errors.append("earliestDivergence 必须为对象")
    else:
        for key in ("stage", "evidence"):
            if not str(divergence.get(key) or "").strip():
                errors.append("earliestDivergence.%s 不能为空" % key)
        if not isinstance(divergence.get("downstreamEffects"), list):
            errors.append("earliestDivergence.downstreamEffects 必须为数组")
    ablation = analysis.get("ablation")
    if not isinstance(ablation, dict):
        errors.append("ablation 必须为对象")
    else:
        for key in ("factor", "baselineResult", "counterfactualResult"):
            if not str(ablation.get(key) or "").strip():
                errors.append("ablation.%s 不能为空" % key)
        if not isinstance(ablation.get("constantsHeld"), list) or not ablation.get("constantsHeld"):
            errors.append("ablation.constantsHeld 不能为空")
    return errors
