#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记录批量人工裁决，并把评测范围剔除与期望修正分开保存。"""

import argparse
import json
import os
import tempfile


VALID_ACTIONS = frozenset(("keep-human", "accept-model", "corrected", "exclude"))


def _read_registry(path, kind):
    if not os.path.isfile(path):
        return {"schemaVersion": 1, "kind": kind, "entries": []}
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("schemaVersion") != 1 or data.get("kind") != kind or not isinstance(data.get("entries"), list):
        raise ValueError("登记表格式无效：%s" % path)
    return data


def _stage_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".case-adjudication-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return temporary
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _restore(path, original):
    if original is None:
        if os.path.exists(path):
            os.unlink(path)
        return
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".case-adjudication-restore-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_pair_atomic(first_path, first_value, second_path, second_value):
    """两个登记表要么都更新，要么恢复原状。"""
    if os.path.normcase(os.path.abspath(first_path)) == os.path.normcase(os.path.abspath(second_path)):
        raise ValueError("裁决登记表与排除登记表必须使用不同路径")
    originals = []
    for path in (first_path, second_path):
        if os.path.exists(path):
            with open(path, "rb") as stream:
                originals.append(stream.read())
        else:
            originals.append(None)
    staged = []
    try:
        staged.append(_stage_json(first_path, first_value))
        staged.append(_stage_json(second_path, second_value))
    except BaseException:
        for temporary in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)
        raise
    committed = []
    try:
        for temporary, path in zip(staged, (first_path, second_path)):
            os.replace(temporary, path)
            committed.append(path)
    except BaseException:
        for path, original in reversed(list(zip((first_path, second_path), originals))):
            if path in committed or os.path.exists(path):
                _restore(path, original)
        raise
    finally:
        for temporary in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)


def validate_decisions(payload):
    """裁决必须来自用户，并对修正与剔除提供可追溯理由。"""
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        return ["case-adjudication.json schemaVersion 必须为 1"]
    errors = []
    if payload.get("reviewedBy") != "user" or not str(payload.get("reviewedAt") or "").strip():
        errors.append("缺少用户审核凭据")
    if not str(payload.get("batchId") or "").strip():
        errors.append("batchId 不能为空")
    rows = payload.get("decisions")
    if not isinstance(rows, list) or not rows:
        return errors + ["decisions 不能为空"]
    seen = set()
    for row in rows:
        case_id = row.get("caseId") if isinstance(row, dict) else None
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append("裁决条目缺少 caseId")
            continue
        if case_id in seen:
            errors.append("caseId 重复：%s" % case_id)
        seen.add(case_id)
        action = row.get("action")
        if action not in VALID_ACTIONS:
            errors.append("%s action 无效" % case_id)
        if not str(row.get("reason") or "").strip():
            errors.append("%s reason 不能为空" % case_id)
        if action in ("accept-model", "corrected") and "approvedExpected" not in row:
            errors.append("%s 缺少 approvedExpected" % case_id)
        if action == "exclude" and not str(row.get("exclusionScope") or "").strip():
            errors.append("%s 缺少 exclusionScope" % case_id)
    return errors


def apply_decisions(payload, adjudication_path, exclusion_path):
    """更新裁决和剔除登记表；不删除正式用例。"""
    errors = validate_decisions(payload)
    if errors:
        raise ValueError("；".join(errors))
    adjudication = _read_registry(adjudication_path, "adjudication")
    exclusions = _read_registry(exclusion_path, "exclusion")
    adjudication_by_id = {row["caseId"]: row for row in adjudication["entries"]}
    exclusion_by_id = {row["caseId"]: row for row in exclusions["entries"]}
    for row in payload["decisions"]:
        entry = dict(row, batchId=payload["batchId"], reviewedBy=payload["reviewedBy"],
                     reviewedAt=payload["reviewedAt"])
        if row["action"] == "exclude":
            exclusion_by_id[row["caseId"]] = entry
            adjudication_by_id.pop(row["caseId"], None)
        else:
            adjudication_by_id[row["caseId"]] = entry
            exclusion_by_id.pop(row["caseId"], None)
    adjudication["entries"] = [adjudication_by_id[key] for key in sorted(adjudication_by_id)]
    exclusions["entries"] = [exclusion_by_id[key] for key in sorted(exclusion_by_id)]
    _write_pair_atomic(adjudication_path, adjudication, exclusion_path, exclusions)
    return {"recorded": len(payload["decisions"]), "adjudicationCount": len(adjudication["entries"]),
            "exclusionCount": len(exclusions["entries"])}


def main():
    parser = argparse.ArgumentParser(description="应用人工用例裁决与评测范围剔除")
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--adjudication-registry", required=True)
    parser.add_argument("--exclusion-registry", required=True)
    args = parser.parse_args()
    with open(args.decisions, encoding="utf-8") as stream:
        payload = json.load(stream)
    result = apply_decisions(payload, args.adjudication_registry, args.exclusion_registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
