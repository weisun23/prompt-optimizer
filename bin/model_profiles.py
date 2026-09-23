#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载并解析 prompt-optimizer 的模型画像。"""

import argparse
import copy
import json
import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REGISTRY_PATH = os.path.join(ROOT, "profiles", "model-profiles.json")


def _read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def load_registry(path=None):
    registry = _read_json(path or DEFAULT_REGISTRY_PATH)
    if registry.get("schemaVersion") != 2 or not isinstance(registry.get("profiles"), list):
        raise ValueError("模型画像知识库格式无效")
    if not isinstance(registry.get("taskTypeDirectives"), dict):
        raise ValueError("模型画像知识库缺少 taskTypeDirectives")
    ids = [item.get("id") for item in registry["profiles"]]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("模型画像 ID 缺失或重复")
    if "generic" not in ids:
        raise ValueError("模型画像知识库缺少 generic")
    for profile in registry["profiles"]:
        if profile.get("consumer") != "host-agent":
            raise ValueError("模型画像 consumer 必须为 host-agent：%s" % profile.get("id"))
        if "*" not in (profile.get("targetRoles") or []):
            raise ValueError("模型画像必须允许宿主优化任意目标角色：%s" % profile.get("id"))
        if not profile.get("taskTypes"):
            raise ValueError("模型画像缺少 taskTypes：%s" % profile.get("id"))
    return registry


def _norm(value):
    return str(value or "").strip().lower()


def _matches_any(value, choices):
    normalized = _norm(value)
    return bool(normalized and normalized in {_norm(item) for item in (choices or [])})


def _matches_pattern(value, patterns):
    normalized = _norm(value)
    return bool(normalized and any(re.search(re.escape(_norm(pattern)), normalized)
                                   for pattern in (patterns or []) if _norm(pattern)))


def _profiles_by_id(registry):
    return {item["id"]: item for item in registry["profiles"]}


def resolve_profile(config, role="candidate", registry=None):
    registry = registry or load_registry()
    profiles = _profiles_by_id(registry)
    generic = profiles["generic"]
    role_cfg = dict(((config.get("models") or {}).get(role)) or {})
    explicit = role_cfg.get("promptProfile")
    if explicit and explicit not in ("auto", "generic"):
        if explicit not in profiles:
            raise ValueError("未知模型画像：%s" % explicit)
        return {"profile": copy.deepcopy(profiles[explicit]), "confidence": "high",
                "matchedBy": ["promptProfile"]}
    if explicit == "generic" or not role_cfg:
        return {"profile": copy.deepcopy(generic), "confidence": "high" if explicit else "none",
                "matchedBy": ["promptProfile"] if explicit else []}

    provider = role_cfg.get("provider")
    family = role_cfg.get("modelFamily")
    model = role_cfg.get("model")
    reasoning = bool(role_cfg.get("reasoning", False))
    candidates = []
    for profile in registry["profiles"]:
        if profile["id"] == "generic":
            continue
        match = profile.get("match") or {}
        if "reasoning" in match and bool(match["reasoning"]) != reasoning:
            continue
        matched_by = []
        score = 0
        if _matches_any(provider, match.get("providers")):
            matched_by.append("provider")
            score += 4
        if _matches_any(family, match.get("modelFamilies")):
            matched_by.append("modelFamily")
            score += 4
        if _matches_pattern(model, match.get("modelPatterns")):
            matched_by.append("model")
            score += 2
        if score:
            candidates.append((score, profile, matched_by))

    if not candidates:
        return {"profile": copy.deepcopy(generic), "confidence": "none", "matchedBy": []}
    candidates.sort(key=lambda item: item[0], reverse=True)
    top_score = candidates[0][0]
    top = [item for item in candidates if item[0] == top_score]
    if len(top) != 1:
        return {"profile": copy.deepcopy(generic), "confidence": "ambiguous",
                "matchedBy": sorted(set(key for _, _, keys in top for key in keys))}
    _, profile, matched_by = top[0]
    confidence = "high" if ("provider" in matched_by and
                            ("modelFamily" in matched_by or "model" in matched_by)) else "medium"
    return {"profile": copy.deepcopy(profile), "confidence": confidence,
            "matchedBy": matched_by}


def infer_prompt_task_type(task, target_role="candidate"):
    """识别被优化提示词类型；显式 promptTaskType 优先。"""
    task = task or {}
    if task.get("promptTaskType"):
        return str(task["promptTaskType"])
    role_defaults = {
        "slotfiller": "slot-filling", "judge": "judge",
        "simulator": "simulator", "generate": "case-generation",
    }
    if target_role in role_defaults:
        return role_defaults[target_role]
    if task.get("tools") or task.get("toolMode") in ("native", "text-protocol"):
        return "tool-agent"
    if task.get("taskType") == "inject":
        return "inject-general"
    if task.get("taskType") == "chat":
        return "chat-general"
    return "generic"


def _applies(rule, role, task_type, default_roles, default_tasks):
    roles = rule.get("targetRoles") or default_roles or ["*"]
    tasks = rule.get("taskTypes") or default_tasks or ["*"]
    return ("*" in roles or role in roles) and ("*" in tasks or task_type in tasks)


def build_recommendation(profile, role="candidate", task_type=None, registry=None):
    registry = registry or load_registry()
    task_type = task_type or infer_prompt_task_type({}, target_role=role)
    target_roles = profile.get("targetRoles") or ["candidate"]
    profile_task_types = profile.get("taskTypes") or ["*"]
    directives = []
    if role in target_roles or "*" in target_roles:
        directives.extend(copy.deepcopy(profile.get("commonOptimizationDirectives") or []))
        directives.extend(copy.deepcopy((registry.get("taskTypeDirectives") or {}).get("generic") or []))
        directives.extend(copy.deepcopy((registry.get("taskTypeDirectives") or {}).get(task_type) or []))
        if "*" in profile_task_types or task_type in profile_task_types:
            directives.extend(copy.deepcopy(profile.get("optimizationDirectives") or []))
        directives.extend(copy.deepcopy((profile.get("taskOptimizationDirectives") or {}).get(task_type) or []))
    seen = set()
    directives = [item for item in directives
                  if item.get("id") and not (item["id"] in seen or seen.add(item["id"]))]
    runtime_requirements = [copy.deepcopy(item) for item in profile.get("runtimeRequirements") or []
                            if _applies(item, role, task_type, target_roles, profile_task_types)]
    return {
        "consumer": profile.get("consumer") or "host-agent",
        "targetRole": role,
        "promptTaskType": task_type,
        "profileId": profile.get("id"),
        "label": profile.get("label"),
        "strength": profile.get("recommendationStrength") or "normal",
        "hostOptimizationDirectives": directives,
        "promptPrinciples": copy.deepcopy(directives),
        "runtimeRequirements": runtime_requirements,
        "avoid": list(profile.get("avoid") or []),
    }


def main():
    parser = argparse.ArgumentParser(description="解析模型画像并输出推荐")
    parser.add_argument("--config", required=True)
    parser.add_argument("--role", default="candidate",
                        choices=["generate", "simulator", "candidate", "slotfiller", "judge"])
    parser.add_argument("--registry", default=None)
    parser.add_argument("--task", default=None, help="task.json；用于过滤提示词任务类型")
    args = parser.parse_args()
    result = resolve_profile(_read_json(args.config), args.role,
                             load_registry(args.registry) if args.registry else None)
    task_type = infer_prompt_task_type(_read_json(args.task) if args.task else {},
                                       target_role=args.role)
    result["recommendation"] = build_recommendation(
        result["profile"], args.role, task_type=task_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
