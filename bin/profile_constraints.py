#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建并验证用户确认的模型画像硬约束快照。"""

import argparse
import copy
import json
import os
import tempfile
from datetime import datetime, timezone

import model_profiles


VALID_STATUSES = ("accepted", "modified", "rejected")
VALID_PREAMBLE_OWNERS = (None, "model", "host")
VALID_TARGET_ROLES = ("generate", "simulator", "candidate", "slotfiller", "judge")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _principles(profile, task_type="tool-agent", target_role="candidate", registry=None):
    if profile.get("hostOptimizationDirectives") is not None:
        directives = profile.get("hostOptimizationDirectives") or []
    else:
        directives = model_profiles.build_recommendation(
            profile, role=target_role, task_type=task_type,
            registry=registry)["hostOptimizationDirectives"]
    return {item["id"]: item for item in directives}


def _all_directive_ids(profile, registry=None):
    registry = registry or model_profiles.load_registry()
    directives = list(profile.get("commonOptimizationDirectives") or [])
    directives.extend(profile.get("optimizationDirectives") or [])
    for rows in (profile.get("taskOptimizationDirectives") or {}).values():
        directives.extend(rows or [])
    for rows in (registry.get("taskTypeDirectives") or {}).values():
        directives.extend(rows or [])
    return {item.get("id") for item in directives if item.get("id")}


def _universal_principles(registry):
    return {item["id"]: item for item in registry.get("universalOptimizationPrinciples") or []}


def build_constraint_snapshot(profile, status, accepted_ids, overrides,
                              preamble_owner=None, accepted_at=None,
                              task_type="tool-agent", target_role="candidate",
                              principle_ids=None, principles_confirmed=False):
    if status not in VALID_STATUSES:
        raise ValueError("status 必须为 accepted、modified 或 rejected")
    if preamble_owner not in VALID_PREAMBLE_OWNERS:
        raise ValueError("preambleOwner 必须为 model 或 host")
    if target_role not in VALID_TARGET_ROLES:
        raise ValueError("targetRole 不受支持：%s" % target_role)
    accepted_ids = list(accepted_ids or [])
    principle_ids = list(principle_ids or [])
    overrides = dict(overrides or {})
    if status == "rejected" and accepted_ids:
        raise ValueError("拒绝画像时不能同时选择硬约束")
    registry = model_profiles.load_registry()
    rules = _principles(profile, task_type=task_type, target_role=target_role, registry=registry)
    universal_rules = _universal_principles(registry)
    unknown_principles = sorted(set(principle_ids) - set(universal_rules))
    if unknown_principles:
        raise ValueError("未知通用优化原则：%s" % "、".join(unknown_principles))
    unavailable = sorted((set(accepted_ids) & _all_directive_ids(profile, registry)) - set(rules))
    if unavailable:
        raise ValueError("画像约束不适用于目标角色或提示词任务类型：%s" % "、".join(unavailable))
    unknown = sorted(set(accepted_ids) - set(rules))
    if unknown:
        raise ValueError("未知画像约束：%s" % "、".join(unknown))
    available_rules = dict(universal_rules)
    available_rules.update(rules)
    unknown_overrides = sorted(set(overrides) - set(available_rules))
    if unknown_overrides:
        raise ValueError("未知画像约束覆盖：%s" % "、".join(unknown_overrides))
    selected_ids = principle_ids + accepted_ids
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("通用原则与模型指令 ID 重复")
    unselected_overrides = sorted(set(overrides) - set(selected_ids))
    if unselected_overrides:
        raise ValueError("用户修改了尚未选择的画像约束：%s" % "、".join(unselected_overrides))

    universal_constraints = []
    for constraint_id in principle_ids:
        rule = copy.deepcopy(universal_rules[constraint_id])
        rule["profileDefaultText"] = rule.get("description") or ""
        rule["text"] = str(overrides.get(constraint_id) or rule.get("description") or "")
        rule["level"] = "required"
        rule["sourceType"] = "universal"
        universal_constraints.append(rule)

    model_constraints = []
    selected = [] if status == "rejected" else accepted_ids
    for constraint_id in selected:
        rule = copy.deepcopy(rules[constraint_id])
        rule["profileDefaultText"] = rule.get("text") or ""
        if constraint_id in overrides:
            rule["text"] = str(overrides[constraint_id])
        rule["level"] = "required"
        rule["sourceType"] = "model-profile"
        model_constraints.append(rule)

    hard_constraints = universal_constraints + model_constraints

    selected_set = set(selected)
    return {
        "schemaVersion": 4,
        "profileId": profile.get("id"),
        "profileLabel": profile.get("label"),
        "profileSource": profile.get("source"),
        "consumer": profile.get("consumer") or "host-agent",
        "targetRole": target_role,
        "promptTaskType": task_type,
        "principlesConfirmed": bool(principles_confirmed),
        "selectedPrincipleIds": principle_ids,
        "selectedModelDirectiveIds": selected,
        "universalConstraints": universal_constraints,
        "modelConstraints": model_constraints,
        "status": status,
        "acceptedAt": accepted_at or _now(),
        "preambleOwner": preamble_owner,
        "hardConstraints": hard_constraints,
        "rejectedRecommendations": [item for item in rules if item not in selected_set],
        "userOverrides": [{"id": item, "text": str(overrides[item])}
                          for item in selected_ids if item in overrides],
    }


def validate_constraint_snapshot(snapshot, registry):
    errors = []
    if not isinstance(snapshot, dict):
        return ["约束快照格式无效"]
    schema_version = snapshot.get("schemaVersion")
    if schema_version not in (1, 2, 3, 4):
        return ["约束快照格式无效或 schemaVersion 不受支持"]
    if schema_version in (1, 2, 3):
        errors.append("旧约束快照必须迁移为 schemaVersion 4 并重新确认完整通用优化原则")
    profiles = {item.get("id"): item for item in registry.get("profiles") or []}
    profile_id = snapshot.get("profileId")
    profile = profiles.get(profile_id)
    if not profile:
        errors.append("未知模型画像：%s" % profile_id)
    status = snapshot.get("status")
    if status not in VALID_STATUSES:
        errors.append("status 必须为 accepted、modified 或 rejected")
    if snapshot.get("preambleOwner") not in VALID_PREAMBLE_OWNERS:
        errors.append("preambleOwner 必须为 model 或 host")
    if snapshot.get("consumer", "host-agent") != "host-agent":
        errors.append("consumer 必须为 host-agent")
    target_role = snapshot.get("targetRole") or "candidate"
    if target_role not in VALID_TARGET_ROLES:
        errors.append("targetRole 不受支持：%s" % target_role)
    constraints = snapshot.get("hardConstraints")
    if not isinstance(constraints, list):
        errors.append("hardConstraints 必须为数组")
        constraints = []
    if status in ("accepted", "modified") and not constraints:
        errors.append("接受画像时至少一个硬约束必须被选择")
    if schema_version == 1 and status in ("accepted", "modified") and constraints:
        errors.append("旧约束快照缺少 promptTaskType，必须迁移为 schemaVersion 4 后再使用")
    if schema_version in (2, 3, 4) and not snapshot.get("promptTaskType"):
        errors.append("约束快照必须包含 promptTaskType")
    if schema_version in (3, 4) and not snapshot.get("principlesConfirmed"):
        errors.append("通用优化原则尚未由用户确认")
    if schema_version in (3, 4):
        selected_ids = set(snapshot.get("selectedPrincipleIds") or []) | set(
            snapshot.get("selectedModelDirectiveIds") or [])
        hard_ids = {item.get("id") for item in constraints
                    if isinstance(item, dict) and item.get("id")}
        if selected_ids != hard_ids:
            errors.append("通用原则和模型指令的选择结果与硬约束不一致")
    if status == "rejected" and constraints:
        errors.append("拒绝画像时 hardConstraints 必须为空")
    known = set(_principles(
        profile, task_type=snapshot.get("promptTaskType") or "generic",
        target_role=target_role, registry=registry)) if profile else set()
    known.update(_universal_principles(registry))
    seen = set()
    for row in constraints:
        if not isinstance(row, dict) or not row.get("id"):
            errors.append("硬约束缺少 id")
            continue
        constraint_id = row["id"]
        if constraint_id in seen:
            errors.append("硬约束 ID 重复：%s" % constraint_id)
        seen.add(constraint_id)
        if profile and constraint_id not in known:
            errors.append("画像中不存在硬约束：%s" % constraint_id)
        if not str(row.get("text") or "").strip():
            errors.append("硬约束文本为空：%s" % constraint_id)
        if row.get("level") != "required":
            errors.append("硬约束 level 必须为 required：%s" % constraint_id)
    return errors


def validate_compliance(snapshot, compliance):
    errors = []
    expected = {item.get("id") for item in snapshot.get("hardConstraints") or []
                if isinstance(item, dict) and item.get("id")}
    rows = compliance if isinstance(compliance, list) else []
    if not isinstance(compliance, list):
        errors.append("constraintCompliance 必须为数组")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            errors.append("constraintCompliance 条目缺少 id")
            continue
        constraint_id = row["id"]
        if constraint_id in seen:
            errors.append("constraintCompliance ID 重复：%s" % constraint_id)
        seen.add(constraint_id)
        if constraint_id not in expected:
            errors.append("constraintCompliance 包含未知约束：%s" % constraint_id)
        if str(row.get("status") or "").upper() != "PASS":
            errors.append("硬约束未通过：%s" % constraint_id)
        if not str(row.get("evidence") or "").strip():
            errors.append("硬约束证据为空：%s" % constraint_id)
    for constraint_id in sorted(expected - seen):
        errors.append("缺少硬约束遵循证据：%s" % constraint_id)
    return errors


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def write_json_atomic(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".constraints-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def main():
    parser = argparse.ArgumentParser(description="模型画像约束快照工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="创建用户选择快照")
    build.add_argument("--profile", required=True)
    build.add_argument("--status", choices=VALID_STATUSES, required=True)
    build.add_argument("--accept", action="append", default=[])
    build.add_argument("--overrides", default=None, help="约束 ID 到用户文本的 JSON 文件")
    build.add_argument("--preamble-owner", choices=["model", "host"], default=None)
    build.add_argument("--task-type", default="tool-agent")
    build.add_argument("--target-role", choices=VALID_TARGET_ROLES, default="candidate")
    build.add_argument("--principle", action="append", default=[],
                       help="用户确认采用的通用优化原则 ID，可重复")
    build.add_argument("--confirm-principles", action="store_true",
                       help="确认 --principle 列表就是用户最终选择")
    build.add_argument("--out", required=True)
    validate = subparsers.add_parser("validate", help="验证快照及优化器遵循证据")
    validate.add_argument("--snapshot", required=True)
    validate.add_argument("--compliance", default=None)
    args = parser.parse_args()

    registry = model_profiles.load_registry()
    if args.command == "build":
        profiles = {item["id"]: item for item in registry["profiles"]}
        if args.profile not in profiles:
            parser.error("未知模型画像：%s" % args.profile)
        snapshot = build_constraint_snapshot(
            profiles[args.profile], args.status, args.accept,
            read_json(args.overrides) if args.overrides else {}, args.preamble_owner,
            task_type=args.task_type, target_role=args.target_role,
            principle_ids=args.principle, principles_confirmed=args.confirm_principles)
        errors = validate_constraint_snapshot(snapshot, registry)
        if errors:
            parser.error("；".join(errors))
        write_json_atomic(args.out, snapshot)
        print("已生成模型画像约束快照：%s" % args.out)
        return

    snapshot = read_json(args.snapshot)
    errors = validate_constraint_snapshot(snapshot, registry)
    if args.compliance:
        errors.extend(validate_compliance(snapshot, read_json(args.compliance)))
    if errors:
        raise SystemExit("约束验证失败：%s" % "；".join(errors))
    print("约束验证通过")


if __name__ == "__main__":
    main()
