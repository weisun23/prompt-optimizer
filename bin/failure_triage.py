#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证评分后的失败归因；不修改正式用例。"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_protocol
import case_taxonomy


FAILURE_OWNERS = (
    "model_failure", "case_error", "device_error", "scorer_error",
    "spec_ambiguous", "tool_contract_error",
)
CASE_STATUSES = ("active", "pending_review", "corrected", "excluded")


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        digest.update(stream.read())
    return digest.hexdigest()


def _already_approved(case_id, correction_registry, case_root):
    if not isinstance(correction_registry, dict) or not case_root:
        return False
    for entry in correction_registry.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("caseId") != case_id or entry.get("status") != "applied":
            continue
        case_path = entry.get("casePath")
        if not isinstance(case_path, str) or not case_path.strip():
            return False
        root = os.path.normcase(os.path.realpath(case_root))
        path = os.path.normcase(os.path.realpath(os.path.join(root, case_path)))
        try:
            in_root = os.path.commonpath([root, path]) == root
        except ValueError:
            in_root = False
        if not in_root or not os.path.isfile(path):
            return False
        return _file_hash(path) == entry.get("correctedSha256")
    return False


def validate_triage(triage, failed_case_ids=None, correction_registry=None, case_root=None,
                    score_rows=None):
    errors = []
    if not isinstance(triage, dict) or triage.get("schemaVersion") != 3:
        return ["failure_triage.json schemaVersion 必须为 3"]
    taxonomy = triage.get("taxonomy")
    taxonomy_errors = case_taxonomy.validate_taxonomy(taxonomy)
    if taxonomy_errors:
        return ["failure_triage taxonomy 无效：%s" % "；".join(taxonomy_errors)]
    items = triage.get("items")
    if not isinstance(items, list) or not items:
        return ["failure_triage.json items 不能为空"]
    seen = set()
    for row in items:
        case_id = row.get("caseId") if isinstance(row, dict) else None
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append("失败归因条目缺少有效 caseId")
            continue
        if case_id in seen:
            errors.append("caseId 重复：%s" % case_id)
        seen.add(case_id)
        if row.get("failureOwner") not in FAILURE_OWNERS:
            errors.append("%s failureOwner 无效" % case_id)
        if row.get("caseStatus") not in CASE_STATUSES:
            errors.append("%s caseStatus 无效" % case_id)
        for error in case_taxonomy.validate_values(taxonomy, row.get("taxonomyValues")):
            errors.append("%s：%s" % (case_id, error))
        stability = row.get("stabilityEvidence")
        if not isinstance(stability, dict):
            errors.append("%s stabilityEvidence 必须为对象" % case_id)
            stability = {}
        counts = {}
        for key in ("validRuns", "failureRuns", "passRuns", "invalidRuns"):
            value = stability.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append("%s stabilityEvidence.%s 必须为非负整数" % (case_id, key))
                value = 0
            counts[key] = value
        if counts["validRuns"] != counts["failureRuns"] + counts["passRuns"]:
            errors.append("%s validRuns 必须等于 failureRuns + passRuns" % case_id)
        total_runs = counts["validRuns"] + counts["invalidRuns"]
        environment_hash = str(stability.get("environmentSha256") or "")
        if total_runs and not re.fullmatch(r"[0-9a-f]{64}", environment_hash):
            errors.append("%s 有运行证据时必须提供 environmentSha256" % case_id)
        stability_label = row.get("stabilityLabel")
        if stability_label not in case_taxonomy.stability_label_ids(taxonomy):
            errors.append("%s stabilityLabel 不在任务分类体系中" % case_id)
        eligible = row.get("optimizationEligible")
        if eligible not in (True, False):
            errors.append("%s optimizationEligible 必须为布尔值" % case_id)
        if eligible is True and (row.get("failureOwner") != "model_failure"
                                 or row.get("confidence") != "high"
                                 or row.get("caseStatus") != "active"):
            errors.append("%s optimizationEligible=true 只允许高置信 active model_failure" % case_id)
        if row.get("confidence") not in ("high", "medium", "low"):
            errors.append("%s confidence 无效" % case_id)
        if not _nonempty_text_list(row.get("evidence")):
            errors.append("%s evidence 不能为空" % case_id)
        if not _nonempty_text_list(row.get("authoritativeBasis")):
            errors.append("%s authoritativeBasis 不能为空，必须引用正式定义或用户确认口径" % case_id)
        context_scope = row.get("contextScope")
        if context_scope not in ("full", "windowed"):
            errors.append("%s contextScope 必须为 full 或 windowed" % case_id)
        if context_scope == "windowed" and not str(row.get("windowDefinition") or "").strip():
            errors.append("%s windowed 归因必须说明 windowDefinition" % case_id)
        neighbors = row.get("neighborCaseIds")
        if not _nonempty_text_list(neighbors):
            errors.append("%s neighborCaseIds 不能为空，必须提供相邻正反边界" % case_id)
        elif case_id in neighbors:
            errors.append("%s neighborCaseIds 不能包含自身" % case_id)
        if _nonempty_text_list(neighbors) and score_rows is not None:
            invalid_neighbors = [neighbor for neighbor in neighbors
                                 if (score_rows.get(neighbor) or {}).get("verdict") != "PASS"]
            if invalid_neighbors:
                errors.append("%s neighborCaseIds 必须来自当前同口径 PASS：%s" % (
                    case_id, "、".join(map(str, invalid_neighbors))))
        if not str(row.get("decisionAnchor") or "").strip():
            errors.append("%s decisionAnchor 不能为空；不涉及时序时明确写 not-applicable 及原因" % case_id)
        fields_used = row.get("fieldsUsed")
        provenance = row.get("fieldProvenance")
        if (not isinstance(fields_used, list)
                or any(not isinstance(field, str) or not field.strip() for field in fields_used)):
            errors.append("%s fieldsUsed 必须为字段名数组，可为空" % case_id)
            fields_used = []
        if not isinstance(provenance, list):
            errors.append("%s fieldProvenance 必须为数组" % case_id)
            provenance = []
        provenance_by_field = {}
        for item in provenance:
            if (not isinstance(item, dict) or not isinstance(item.get("field"), str)
                    or not item.get("field").strip()):
                errors.append("%s fieldProvenance 条目缺少 field" % case_id)
                continue
            field = item["field"]
            if field in provenance_by_field:
                errors.append("%s fieldProvenance 字段重复：%s" % (case_id, field))
            provenance_by_field[field] = item
            for key in ("definitionSource", "producer", "writeOrDerivationPath"):
                if not str(item.get(key) or "").strip():
                    errors.append("%s 字段 %s 缺少 %s" % (case_id, field, key))
        missing_provenance = sorted(set(fields_used) - set(provenance_by_field))
        if missing_provenance:
            errors.append("%s 以下字段缺少来源与写入链路：%s" % (
                case_id, "、".join(missing_provenance)))
        if not str(row.get("action") or "").strip():
            errors.append("%s action 不能为空" % case_id)
        if row.get("failureOwner") == "case_error":
            if row.get("caseStatus") != "pending_review":
                errors.append("%s case_error 的 caseStatus 必须为 pending_review" % case_id)
            if _already_approved(case_id, correction_registry, case_root):
                errors.append("%s 已批准修正且内容未变化，无需重复修正" % case_id)
            if not str(row.get("proposedCasePath") or "").strip():
                errors.append("%s case_error 必须提供 proposedCasePath" % case_id)
            if row.get("reviewStatus") != "pending_review":
                errors.append("%s case_error 初始状态必须为 pending_review" % case_id)
    if failed_case_ids is not None:
        expected = set(failed_case_ids)
        actual = {row.get("caseId") for row in items
                  if isinstance(row, dict) and isinstance(row.get("caseId"), str)
                  and row.get("caseId").strip()}
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing:
            errors.append("失败归因未覆盖用例：%s" % "、".join(missing))
        if extra:
            errors.append("失败归因包含非失败用例：%s" % "、".join(extra))
    return errors


def _nonempty_text_list(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))


def model_failure_case_ids(triage):
    return [row["caseId"] for row in triage.get("items") or []
            if row.get("failureOwner") == "model_failure" and row.get("confidence") == "high"
            and row.get("caseStatus") == "active" and row.get("optimizationEligible") is True]


def main():
    parser = argparse.ArgumentParser(description="验证失败用例归因")
    parser.add_argument("--triage", required=True)
    parser.add_argument("--scores", required=True, help="评分目录；强制覆盖全部 FAIL 用例")
    parser.add_argument("--registry", default=None, help="case_corrections_registry.json")
    parser.add_argument("--case-root", default=None)
    args = parser.parse_args()
    with open(args.triage, encoding="utf-8") as stream:
        triage = json.load(stream)
    failed_case_ids = None
    if args.scores:
        rows, _ = score_protocol.load_score_round(args.scores)
        failed_case_ids = {case_id for case_id, row in rows.items() if row.get("verdict") == "FAIL"}
    correction_registry = None
    if args.registry:
        with open(args.registry, encoding="utf-8") as stream:
            correction_registry = json.load(stream)
    errors = validate_triage(
        triage, failed_case_ids, correction_registry, args.case_root,
        score_rows=rows if args.scores else None)
    if errors:
        raise SystemExit("失败归因无效：%s" % "；".join(errors))
    print(json.dumps({"modelFailureCaseIds": model_failure_case_ids(triage)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
