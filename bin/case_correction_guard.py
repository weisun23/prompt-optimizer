#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅在人工批准和哈希一致时应用用例修正，并记录复用凭据。"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import failure_triage


APPROVED_STATUSES = ("approved", "modified_and_approved")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now():
    return _now()


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(case_root, relative):
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("用例路径无效：%s" % relative)
    root = os.path.normcase(os.path.realpath(case_root))
    path = os.path.normcase(os.path.realpath(os.path.join(root, relative or "")))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("用例路径越界：%s" % relative)
    return path


def _backup_filename(case_id, original_sha256):
    case_token = hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()[:16]
    return "%s-%s.json" % (case_token, str(original_sha256)[:12])


def _load_registry(path):
    if not os.path.isfile(path):
        return {"schemaVersion": 1, "entries": []}
    data = read_json(path)
    if data.get("schemaVersion") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("case_corrections_registry.json 格式无效")
    return data


def _write_atomic(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".case-correction-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def write_json_atomic(path, value):
    """供运行器完成重建基线标记时复用同一原子写协议。"""
    _write_atomic(path, value)


def _stage_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".case-stage-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return temp_path
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _restore_backup(backup_path, case_path):
    directory = os.path.dirname(os.path.abspath(case_path))
    fd, temp_path = tempfile.mkstemp(prefix=".case-rollback-", suffix=".json", dir=directory)
    os.close(fd)
    try:
        shutil.copy2(backup_path, temp_path)
        os.replace(temp_path, case_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _file_state(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as stream:
        return stream.read()


def _restore_file_state(path, state):
    if state is None:
        if os.path.exists(path):
            os.unlink(path)
        return
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".metadata-rollback-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(state)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _registry_by_case(registry):
    return {row.get("caseId"): row for row in registry.get("entries") or [] if row.get("caseId")}


def validate_review(review, case_root, registry=None):
    errors = []
    if not isinstance(review, dict) or review.get("schemaVersion") != 2:
        return ["case-correction-review.json schemaVersion 必须为 2，旧审核文件必须重新生成"]
    rows = review.get("reviews")
    if not isinstance(rows, list) or not rows:
        return ["reviews 不能为空"]
    if not str(review.get("approvalStatement") or "").strip():
        errors.append("缺少用户确认的 approvalStatement")
    triage = None
    try:
        triage_path = _safe_path(case_root, review.get("triagePath"))
        if not os.path.isfile(triage_path):
            errors.append("失败归因文件不存在")
        elif file_hash(triage_path) != review.get("triageSha256"):
            errors.append("失败归因文件哈希与审核记录不一致")
        else:
            triage = read_json(triage_path)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        errors.append("失败归因文件无效：%s" % error)
    triage_rows = {}
    if not isinstance(triage, dict) or triage.get("schemaVersion") != 3 or not isinstance(triage.get("items"), list):
        if triage is not None:
            errors.append("失败归因文件格式无效")
    else:
        errors.extend(failure_triage.validate_triage(triage))
        triage_rows = {item.get("caseId"): item for item in triage["items"]
                       if isinstance(item, dict) and isinstance(item.get("caseId"), str)
                       and item.get("caseId").strip()}
    registry = registry or {"schemaVersion": 1, "entries": []}
    approved_by_case = _registry_by_case(registry)
    seen = set()
    for row in rows:
        case_id = row.get("caseId") if isinstance(row, dict) else None
        if not case_id:
            errors.append("审核条目缺少 caseId")
            continue
        if case_id in seen:
            errors.append("审核 caseId 重复：%s" % case_id)
        seen.add(case_id)
        if row.get("reviewStatus") not in APPROVED_STATUSES:
            errors.append("%s 未经人工批准，reviewStatus 必须为 approved 或 modified_and_approved" % case_id)
        if row.get("reviewedBy") != "user" or not row.get("reviewedAt"):
            errors.append("%s 缺少用户人工审核凭据" % case_id)
        triage_row = triage_rows.get(case_id)
        if not triage_row:
            errors.append("%s 未在失败归因文件中找到" % case_id)
        elif (triage_row.get("failureOwner") != "case_error" or
              triage_row.get("reviewStatus") != "pending_review" or
              triage_row.get("proposedCasePath") != row.get("proposalPath")):
            errors.append("%s 与失败归因提案链不一致" % case_id)
        try:
            case_path = _safe_path(case_root, row.get("casePath"))
            proposal_path = _safe_path(case_root, row.get("proposalPath"))
        except ValueError as error:
            errors.append(str(error))
            continue
        if not os.path.isfile(case_path) or not os.path.isfile(proposal_path):
            errors.append("%s 正式用例或修正提案不存在" % case_id)
            continue
        current_hash = file_hash(case_path)
        registered = approved_by_case.get(case_id)
        if registered:
            if current_hash == registered.get("correctedSha256"):
                continue
            errors.append("%s 已审核修正后的用例内容已变化，必须重新审核" % case_id)
            continue
        if current_hash != row.get("originalSha256"):
            errors.append("%s 原始用例哈希不一致，必须重新审核" % case_id)
        if file_hash(proposal_path) != row.get("proposedSha256"):
            errors.append("%s 修正提案哈希不一致，必须重新审核" % case_id)
        try:
            if read_json(case_path).get("id") != case_id or read_json(proposal_path).get("id") != case_id:
                errors.append("%s 用例内容中的 id 不一致" % case_id)
        except (ValueError, TypeError, AttributeError):
            errors.append("%s 正式用例或修正提案不是有效 JSON 对象" % case_id)
    return errors


def case_set_snapshot(case_root):
    digest = hashlib.sha256()
    ignored = {"proposed-corrections", "correction-backups"}
    files = []
    for directory, dirs, names in os.walk(case_root):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in names:
            if not name.endswith(".json") or name in (
                    "case_corrections_registry.json", "case-correction-review.json",
                    "failure-triage.json", "failure_triage.json", "case-correction-receipt.json",
                    ".rebaseline-required.json"):
                continue
            path = os.path.join(directory, name)
            files.append((os.path.relpath(path, case_root).replace("\\", "/"), path))
    case_ids = []
    for relative, path in sorted(files):
        data = read_json(path)
        rows = data if isinstance(data, list) else [data]
        file_case_ids = [row.get("id") for row in rows
                         if isinstance(row, dict) and row.get("id")]
        if not file_case_ids:
            continue
        case_ids.extend(file_case_ids)
        digest.update(relative.encode("utf-8")); digest.update(b"\0")
        with open(path, "rb") as stream:
            digest.update(stream.read())
        digest.update(b"\0")
    duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicates:
        raise ValueError("用例 ID 重复，无法生成用例集快照：%s" % "、".join(duplicates))
    return {"sha256": digest.hexdigest(), "count": len(case_ids),
            "caseIds": sorted(case_ids)}


def case_set_hash(case_root):
    return case_set_snapshot(case_root)["sha256"]


def apply_review(review_path, case_root, registry_path=None, backup_dir=None, receipt_path=None):
    registry_path = _safe_path(
        case_root, registry_path or os.path.join(case_root, "case_corrections_registry.json"))
    backup_dir = _safe_path(case_root, backup_dir or os.path.join(case_root, "correction-backups"))
    marker_path = _safe_path(case_root, os.path.join(case_root, ".rebaseline-required.json"))
    registry_state = _file_state(registry_path)
    marker_state = _file_state(marker_path)
    receipt_state = _file_state(receipt_path) if receipt_path else None
    review = read_json(review_path)
    review_sha256 = file_hash(review_path)
    registry = _load_registry(registry_path)
    errors = validate_review(review, case_root, registry)
    if errors:
        raise ValueError("；".join(errors))
    by_case = _registry_by_case(registry)
    applied, skipped, operations = [], [], []
    replaced = []
    previous_marker = read_json(marker_path) if os.path.isfile(marker_path) else None
    try:
        for row in review["reviews"]:
            case_id = row["caseId"]
            case_path = _safe_path(case_root, row["casePath"])
            if case_id in by_case and file_hash(case_path) == by_case[case_id].get("correctedSha256"):
                skipped.append(case_id)
                continue
            proposal_path = _safe_path(case_root, row["proposalPath"])
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, _backup_filename(case_id, row["originalSha256"]))
            if os.path.exists(backup_path):
                if file_hash(backup_path) != row["originalSha256"]:
                    raise ValueError("%s 备份文件哈希异常，拒绝使用已污染备份" % case_id)
            else:
                shutil.copy2(case_path, backup_path)
            corrected = read_json(proposal_path)
            staged_path = _stage_json(case_path, corrected)
            operations.append({"row": row, "casePath": case_path, "backupPath": backup_path,
                               "stagedPath": staged_path})
        for operation in operations:
            os.replace(operation["stagedPath"], operation["casePath"])
            replaced.append(operation)
        for operation in operations:
            row = operation["row"]
            case_id = row["caseId"]
            entry = {"caseId": case_id, "status": "applied", "reviewStatus": row["reviewStatus"],
                     "reviewedBy": row["reviewedBy"], "reviewedAt": row["reviewedAt"],
                     "appliedAt": _now(), "casePath": row["casePath"],
                     "originalSha256": row["originalSha256"],
                     "correctedSha256": file_hash(operation["casePath"]),
                     "triageSha256": review.get("triageSha256"),
                     "reviewSha256": review_sha256,
                     "approvalStatement": review.get("approvalStatement"),
                     "reason": row.get("reason") or ""}
            by_case[case_id] = entry
            applied.append(case_id)
        registry["entries"] = [by_case[key] for key in sorted(by_case)]
        _write_atomic(registry_path, registry)
        case_snapshot = case_set_snapshot(case_root)
        case_set_sha256 = case_snapshot["sha256"]
        previous_pending = isinstance(previous_marker, dict) and previous_marker.get("status") == "pending"
        if previous_pending and not applied and previous_marker.get("caseSetSha256") != case_set_sha256:
            raise ValueError("待重建基线期间用例集已变化，必须重新审核并生成修正收据")
        pending_case_ids = list(previous_marker.get("appliedCaseIds") or []) if previous_pending else []
        pending_case_ids = sorted(set(pending_case_ids + applied))
        if applied or previous_pending:
            _write_atomic(marker_path, {"schemaVersion": 1, "status": "pending",
                          "caseSetSha256": case_set_sha256, "appliedCaseIds": pending_case_ids,
                          "caseCount": case_snapshot["count"], "caseIds": case_snapshot["caseIds"],
                          "triageSha256": review.get("triageSha256"),
                          "reviewSha256": review_sha256,
                          "createdAt": (previous_marker or {}).get("createdAt") or _now()})
        requires_rebaseline = bool(applied or previous_pending)
        receipt = {"schemaVersion": 1, "appliedCaseIds": pending_case_ids,
                   "newlyAppliedCaseIds": applied, "skippedAlreadyApproved": skipped,
                   "requiresRebaseline": requires_rebaseline,
                   "caseSetSha256": case_set_sha256, "registryPath": registry_path,
                   "caseCount": case_snapshot["count"], "caseIds": case_snapshot["caseIds"],
                   "markerPath": marker_path if requires_rebaseline else None,
                   "triageSha256": review.get("triageSha256"), "reviewSha256": review_sha256}
        if receipt_path:
            _write_atomic(receipt_path, receipt)
    except BaseException:
        rollback_errors = []
        for operation in reversed(replaced):
            try:
                _restore_backup(operation["backupPath"], operation["casePath"])
            except Exception as error:
                rollback_errors.append(str(error))
        states = [(registry_path, registry_state), (marker_path, marker_state)]
        if receipt_path:
            states.append((receipt_path, receipt_state))
        for path, state in states:
            try:
                _restore_file_state(path, state)
            except Exception as error:
                rollback_errors.append("%s：%s" % (path, error))
        if rollback_errors:
            raise RuntimeError("批量应用失败且回滚不完整：%s" % "；".join(rollback_errors))
        raise
    finally:
        for operation in operations:
            if os.path.exists(operation["stagedPath"]):
                os.unlink(operation["stagedPath"])
    return receipt


def main():
    parser = argparse.ArgumentParser(description="应用人工批准的用例修正")
    parser.add_argument("--review", required=True)
    parser.add_argument("--case-root", required=True)
    parser.add_argument("--registry", default=None)
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    receipt = apply_review(
        args.review, args.case_root, args.registry, args.backup_dir, receipt_path=args.receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
