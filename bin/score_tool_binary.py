#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅按函数调用正确性进行0/100二值评分。"""

import argparse
import concurrent.futures
import copy
import json
import os
import sys
import threading

import llm
import scorer
import score_protocol
import run_manifest


PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def load_binary_prompt():
    path = os.path.join(PROMPTS_DIR, "tool_binary_score.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


EMPTY_OPTIONAL_VALUES = {None, "", "无", "无特殊需求", "没有特殊需求"}


def normalize_binary(value):
    """任何非精确100的评分都按0处理。"""
    try:
        return 100 if float(value) == 100 else 0
    except (TypeError, ValueError):
        return 0


def strict_aggregate(values):
    """多次评分必须全部为100，该用例才得100。"""
    return 100 if values and all(normalize_binary(value) == 100 for value in values) else 0


def binary_result(case_id, score, reason, errors, judge_called, **extra):
    """生成兼容旧消费者与统一评分协议的二值结果。"""
    return score_protocol.make_binary_score(
        case_id, normalize_binary(score), reason, errors,
        judge_called=judge_called, **extra
    )


def invalid_result(case_id, reason, errors, **extra):
    """装置错误不参与二值统计，也不调用 Judge。"""
    row = {
        "caseId": case_id,
        "scoreMode": "tool-binary",
        "score": None,
        "totalScore": None,
        "verdict": "INVALID",
        "failLabel": "device_error",
        "reason": reason or "测试装置错误",
        "errors": list(errors or []),
        "judgeCalled": False,
    }
    row.update(extra)
    return row


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def expected_names(case):
    names = case.get("toolsExpect") or []
    if isinstance(names, str):
        names = [names]
    return [name for name in names if isinstance(name, str) and name.strip()]


def normalize_optional_contract(task, case):
    """空可选字段省略、传空字符串或“无”按等价处理。"""
    normalized_task = copy.deepcopy(task)
    empty_optional_fields = set(normalized_task.get("emptyEquivalentToolArgs") or [])
    ignored = set(normalized_task.get("systemToolArgs") or [])
    ignored.update(empty_optional_fields)
    normalized_task["systemToolArgs"] = sorted(ignored)
    normalized_case = copy.deepcopy(case)
    expected = normalized_case.get("expectedToolArgs") or {}
    for value in expected.values():
        groups = value if isinstance(value, list) else [value]
        for group in groups:
            if not isinstance(group, dict):
                continue
            for field in empty_optional_fields:
                if field in group and group[field] in EMPTY_OPTIONAL_VALUES:
                    group.pop(field)
    return normalized_task, normalized_case


def deterministic_errors(case, traj, check_text):
    """名称、次数、顺序和测试契约异常直接判0；参数差异交给语义评分。"""
    expected = expected_names(case)
    actual = [item.get("name") for item in scorer.actual_tool_calls(traj)]
    errors = []
    if actual != expected:
        errors.append("函数名称/次数/顺序不符：期望%s，实际%s" % (expected, actual))
    if "⚠️" in check_text:
        errors.extend(line.strip() for line in check_text.splitlines() if "⚠️" in line)
    policy = case.get("expectedCallPolicy") or {}
    events = traj.get("toolCalls") or []
    turns = [item.get("assistantTurn") for item in events if isinstance(item, dict)
             and isinstance(item.get("assistantTurn"), int)]
    if expected and policy.get("mode") == "immediate" and (not turns or min(turns) != 1):
        errors.append("expectedCallPolicy=immediate，但首次工具调用不在第1个助手轮次")
    if expected and policy.get("mode") in ("after-confirmation", "eventual") and turns:
        max_turn = 1 + int(policy.get("maxAdditionalTurns") or 0)
        if max(turns) > max_turn:
            errors.append("工具调用超过 expectedCallPolicy.maxAdditionalTurns=%d" % (
                policy.get("maxAdditionalTurns") or 0))
    return errors


def score_one(config, task, case, traj, timeout):
    device_errors = traj.get("deviceErrors") or []
    if device_errors or traj.get("endReason") == "device_error":
        messages = [str(item.get("message") or item) if isinstance(item, dict) else str(item)
                    for item in device_errors]
        reason = "；".join(messages) or "测试装置错误"
        return invalid_result(case.get("id") or traj.get("caseId"), reason, device_errors,
                              endReason=traj.get("endReason"))

    normalized_task, normalized_case = normalize_optional_contract(task, case)
    expected_text, check_text = scorer.tool_check(normalized_case, traj, normalized_task)
    actual = scorer.actual_tool_calls(traj)
    errors = deterministic_errors(case, traj, check_text)
    if errors:
        return binary_result(case.get("id"), 0, errors[0], errors, False,
                             toolCheck=check_text)

    if not (normalized_task.get("semanticToolArgFields") or []):
        return binary_result(case.get("id"), 100, "函数名称、次数、顺序、时机和精确参数均一致",
                             [], False, toolCheck=check_text,
                             comparisonMethod="deterministic")

    inp, output = scorer.traj_io(traj)
    flow = (case.get("chat") or {}).get("flowGuide") or case.get("flowGuide") or []
    prompt = (load_binary_prompt()
              .replace("{scenario}", case.get("scenario") or traj.get("scenario") or "（无）")
              .replace("{flowGuide}", json.dumps(flow, ensure_ascii=False, indent=1) if flow else "（无）")
              .replace("{input}", inp)
              .replace("{output}", output)
              .replace("{expectedTools}", expected_text)
              .replace("{toolCheck}", check_text)
              .replace("{actualTools}", json.dumps(actual, ensure_ascii=False, indent=2)))
    reply = llm.chat(config, "judge", [{"role": "user", "content": prompt}],
                     temperature=0, timeout=timeout)
    data = scorer.parse_score(reply.get("content") or "")
    score = normalize_binary(data.get("score", data.get("totalScore")))
    judge_errors = data.get("errors") or []
    if isinstance(judge_errors, str):
        judge_errors = [judge_errors]
    return binary_result(
        case.get("id"), score,
        data.get("reason") or ("函数调用完全正确" if score == 100 else "函数调用不满足二值满分条件"),
        judge_errors, True, toolCheck=check_text,
    )


def run_selftest():
    print(json.dumps({
        "normalize100": normalize_binary(100),
        "normalize80": normalize_binary(80),
        "normalize0": normalize_binary(0),
        "unanimous": strict_aggregate([100, 100]),
        "mixed": strict_aggregate([100, 0]),
        "allZero": strict_aggregate([0, 0]),
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="函数调用0/100二值评分器")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--task")
    parser.add_argument("--trajectories")
    parser.add_argument("--cases")
    parser.add_argument("--out")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--manifest", default=None, help="run_manifest.json 路径；缺省从输出目录推断")
    parser.add_argument("--run-id", default=None, help="要回填评分信息的运行清单 invocation ID")
    args = parser.parse_args()
    if args.selftest:
        run_selftest()
        return
    for name in ("config", "task", "trajectories", "cases", "out"):
        if not getattr(args, name):
            parser.error("--%s 必填" % name)

    config = read_json(args.config)
    task = read_json(args.task)
    cases = scorer.load_cases_map(args.cases)
    trajectories = {}
    for filename in sorted(os.listdir(args.trajectories)):
        if not filename.endswith(".json"):
            continue
        traj = read_json(os.path.join(args.trajectories, filename))
        case_id = traj.get("caseId") or os.path.splitext(filename)[0]
        if case_id in cases:
            trajectories[case_id] = traj

    os.makedirs(args.out, exist_ok=True)
    lock = threading.Lock()
    done = [0]

    def worker(case_id):
        result = score_one(config, task, cases[case_id], trajectories[case_id], args.timeout)
        result["taxonomyValues"] = cases[case_id].get("taxonomyValues") or {}
        result["caseRole"] = cases[case_id].get("caseRole") or ""
        write_json(os.path.join(args.out, "%s.json" % case_id), result)
        with lock:
            done[0] += 1
            display_score = "INVALID" if result.get("verdict") == "INVALID" else str(result["score"])
            print("[%d/%d] %s → %s" % (done[0], len(trajectories), case_id, display_score), flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        results = list(executor.map(worker, sorted(trajectories)))
    summary = score_protocol.summarize_scores({r["caseId"]: r for r in results})
    summary.update({
        "scoreMode": "tool-binary", "evalMode": "tool-binary",
        "binaryValues": [0, 100],
        "judgeCalls": sum(1 for result in results if result.get("judgeCalled")),
    })
    summary["comparisonMethod"] = "judge" if summary["judgeCalls"] else "deterministic"
    write_json(os.path.join(args.out, "_summary.json"), summary)
    run_dir = run_manifest.find_run_root(args.out)
    manifest_path = os.path.abspath(args.manifest) if args.manifest else os.path.join(run_dir, "run_manifest.json")
    version_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as f:
            scorer_version = f.read().strip()
    else:
        scorer_version = "unknown"
    run_manifest.record_scoring(
        manifest_path, args.trajectories, args.out, "score_tool_binary.py",
        "tool-binary", scorer_version, run_id=args.run_id,
        comparison_method=summary["comparisonMethod"], judge_calls=summary["judgeCalls"],
    )
    print("函数调用二值分：%s（%d/%d）" % (
        summary["score"], summary["correctCount"], summary["scoredCount"]))


if __name__ == "__main__":
    main()
