#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一解析评分模式，避免确定性任务误用 Judge。"""

import argparse
import json


DETERMINISTIC_TASK_TYPES = frozenset((
    "classification", "slot-filling", "fixed-json", "extraction",
))
VALID_EVAL_MODES = frozenset(("auto", "judge", "expected", "expected-semantic", "tool-binary"))


def resolve_eval_mode(task):
    task = task if isinstance(task, dict) else {}
    configured = str(task.get("evalMode") or "auto").strip().lower()
    if configured not in VALID_EVAL_MODES:
        raise ValueError("evalMode 不受支持：%s" % configured)
    if configured != "auto":
        return configured
    prompt_task_type = str(task.get("promptTaskType") or "").strip().lower()
    if prompt_task_type in DETERMINISTIC_TASK_TYPES:
        return "expected"
    if task.get("taskType") == "inject" or task.get("userTemplate"):
        return "expected-semantic"
    return "judge"


def uses_judge(eval_mode, task=None):
    if eval_mode == "tool-binary":
        return bool((task or {}).get("semanticToolArgFields") or [])
    return eval_mode in ("judge", "expected-semantic")


def score_mode(eval_mode):
    if eval_mode in ("expected", "expected-semantic"):
        return eval_mode
    return "rubric" if eval_mode == "judge" else eval_mode


def describe_evaluation(task):
    mode = resolve_eval_mode(task)
    judge_required = uses_judge(mode, task)
    return {
        "evalMode": mode,
        "comparisonMethod": "judge" if judge_required else "deterministic",
        "scoringJudgeRequired": judge_required,
        "judgeTimesPerCase": 0 if not judge_required else (1 if mode == "tool-binary" else None),
    }


def main():
    parser = argparse.ArgumentParser(description="解析任务的评分模式与评分模型需求")
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    with open(args.task, encoding="utf-8") as stream:
        task = json.load(stream)
    print(json.dumps(describe_evaluation(task), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
