#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词候选版本、实验账本、恢复状态与人类可读摘要。"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone

import iteration_guard


VALID_DECISIONS = frozenset(("accepted", "rolled_back", "continue", "inconclusive", "experimental"))
VALID_STATUSES = frozenset(("IN_PROGRESS", "CANDIDATE_REJECTED_CONTINUE", "ACCEPTED",
                            "USER_STOPPED", "BUDGET_EXHAUSTED", "BLOCKED"))
STATE_BY_DECISION = {
    "accepted": "ACCEPTED",
    "rolled_back": "CANDIDATE_REJECTED_CONTINUE",
    "continue": "IN_PROGRESS",
    "inconclusive": "IN_PROGRESS",
    "experimental": "IN_PROGRESS",
}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _write_atomic(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".optimization-journal-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_text_atomic(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".optimization-summary-", suffix=".md", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _inside_run(run_dir, reference, require_exists=True):
    run_root = os.path.realpath(os.path.abspath(run_dir))
    path = reference if os.path.isabs(reference) else os.path.join(run_root, reference)
    resolved = os.path.realpath(os.path.abspath(path))
    try:
        inside = os.path.commonpath((run_root, resolved)) == run_root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("文件必须位于运行目录内：%s" % reference)
    if require_exists and not os.path.isfile(resolved):
        raise ValueError("文件不存在：%s" % reference)
    return resolved


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _events_path(run_dir):
    return os.path.join(os.path.abspath(run_dir), "rounds.json")


def _state_path(run_dir):
    return os.path.join(os.path.abspath(run_dir), "optimization-state.json")


def _summary_path(run_dir):
    return os.path.join(os.path.abspath(run_dir), "OPTIMIZATION_SUMMARY.md")


def _load_events(run_dir):
    events = _read_json(_events_path(run_dir), [])
    if not isinstance(events, list):
        raise ValueError("rounds.json 必须为数组")
    return events


def _load_state(run_dir):
    raw = _read_json(_state_path(run_dir), {})
    if not isinstance(raw, dict):
        raise ValueError("optimization-state.json 必须为对象")
    return {
        "schemaVersion": 2,
        "status": raw.get("status") or "IN_PROGRESS",
        "currentBestPrompt": raw.get("currentBestPrompt") or "",
        "currentCandidate": raw.get("currentCandidate") or "",
        "activeAttemptId": raw.get("activeAttemptId") or "",
        "lastCompletedAttemptId": raw.get("lastCompletedAttemptId") or "",
        "actionableRootCauses": list(raw.get("actionableRootCauses") or []),
        "untestedHypotheses": list(raw.get("untestedHypotheses") or []),
        "newEvidenceAvailable": bool(raw.get("newEvidenceAvailable", False)),
        "verifiedConclusions": list(raw.get("verifiedConclusions") or []),
        "rejectedDirections": list(raw.get("rejectedDirections") or []),
        "nextAction": raw.get("nextAction") or "",
        "lastIterationSummary": raw.get("lastIterationSummary") or "",
        "updatedAt": raw.get("updatedAt") or "",
    }


def _next_prompt_version(run_dir):
    versions = []
    for path in glob.glob(os.path.join(run_dir, "prompt_v*.md")):
        match = re.search(r"prompt_v(\d+)\.md$", path)
        if match:
            versions.append(int(match.group(1)))
    return (max(versions) if versions else 0) + 1


def _next_attempt_id(events):
    values = []
    for event in events:
        match = re.fullmatch(r"attempt-(\d+)", str((event or {}).get("attemptId") or ""))
        if match:
            values.append(int(match.group(1)))
    return "attempt-%03d" % ((max(values) if values else 0) + 1)


def _table_text(value):
    if isinstance(value, list):
        value = "、".join(str(item) for item in value)
    return str(value or "-").replace("|", "\\|").replace("\n", " ")


def render_summary(run_dir):
    """从账本与当前状态生成恢复摘要；摘要不是独立事实源。"""
    run_dir = os.path.abspath(run_dir)
    events = _load_events(run_dir)
    state = _load_state(run_dir)
    starts = {event.get("attemptId"): event for event in events
              if isinstance(event, dict) and event.get("eventType") == "attempt-started"}
    completions = [event for event in events
                   if isinstance(event, dict) and event.get("eventType") == "attempt-completed"]
    completed_ids = {event.get("attemptId") for event in completions}
    active = [event for attempt_id, event in starts.items() if attempt_id not in completed_ids]
    milestones = [event for event in events
                  if isinstance(event, dict) and event.get("eventType") == "milestone"]
    lines = [
        "# 提示词优化恢复摘要",
        "",
        "> 本文档由 `optimization_journal.py` 根据 `rounds.json` 和 "
        "`optimization-state.json` 生成。运行事实仍以 manifest、评分文件和哈希为准。",
        "",
        "## 当前状态",
        "",
        "- 更新时间：`%s`" % _table_text(state.get("updatedAt") or _now()),
        "- 工作流状态：`%s`" % _table_text(state.get("status")),
        "- 当前最佳版本：`%s`" % _table_text(state.get("currentBestPrompt")),
        "- 当前候选版本：`%s`" % _table_text(state.get("currentCandidate")),
        "- 最近完成尝试：`%s`" % _table_text(state.get("lastCompletedAttemptId")),
        "- 下一步：%s" % _table_text(state.get("nextAction")),
        "",
        "## 当前进行中",
        "",
    ]
    if active:
        for event in active:
            lines.append("- `%s`：`%s`，根因：%s，计划改动：%s" % (
                _table_text(event.get("attemptId")), _table_text(event.get("version")),
                _table_text(event.get("rootCause")), _table_text(event.get("changeSummary"))))
    else:
        lines.append("- 无未完成的提示词候选。")
    lines.extend(["", "## 已完成尝试", "",
                  "| 尝试 | 版本 | 根因 | 决策 | 结论 | 下一步 |",
                  "|---|---|---|---|---|---|"])
    if completions:
        for event in completions:
            lines.append("| `%s` | `%s` | %s | `%s` | %s | %s |" % (
                _table_text(event.get("attemptId")), _table_text(event.get("version")),
                _table_text(event.get("rootCause")), _table_text(event.get("decision")),
                _table_text(event.get("conclusion")), _table_text(event.get("nextAction"))))
    else:
        lines.append("| - | - | - | - | 尚无已完成尝试 | - |")
    lines.extend(["", "## 里程碑与统计口径变化", ""])
    if milestones:
        for event in milestones:
            lines.append("- `%s` **%s**：%s；下一步：%s" % (
                _table_text(event.get("recordedAt")), _table_text(event.get("milestoneType")),
                _table_text(event.get("conclusion")), _table_text(event.get("nextAction"))))
    else:
        lines.append("- 暂无单独里程碑记录。")
    lines.extend(["", "## 待处理", "",
                  "- 可行动根因：%s" % _table_text(state.get("actionableRootCauses")),
                  "- 未验证假设：%s" % _table_text(state.get("untestedHypotheses")),
                  "- 最新迭代摘要：%s" % _table_text(state.get("lastIterationSummary")),
                  "", "## 权威产物", "",
                  "- 运行清单：`run_manifest.json`",
                  "- 实验账本：`rounds.json`",
                  "- 当前状态：`optimization-state.json`",
                  "- 提示词版本：`prompt_vN.md`",
                  "- 轨迹与评分：`trajectories-*`、`scores/*`",
                  ""])
    _write_text_atomic(_summary_path(run_dir), "\n".join(lines))
    return _summary_path(run_dir)


def start_attempt(run_dir, parent_version, plan_path, change_summary, parent_tools=None):
    """从父版本复制出新的 prompt_vN.md，并追加 attempt-started 事件。"""
    run_dir = os.path.abspath(run_dir)
    if not str(change_summary or "").strip():
        raise ValueError("changeSummary 不能为空")
    parent_path = _inside_run(run_dir, parent_version)
    if not re.fullmatch(r"prompt_v\d+\.md", os.path.basename(parent_path)):
        raise ValueError("父提示词必须命名为 prompt_vN.md")
    plan_path = _inside_run(run_dir, plan_path)
    plan = _read_json(plan_path, {})
    plan_errors = iteration_guard.validate_plan(plan)
    if plan_errors:
        raise ValueError("迭代计划无效：%s" % "；".join(plan_errors))
    events = _load_events(run_dir)
    if any(event.get("eventType") == "attempt-started" and event.get("attemptId")
           not in {item.get("attemptId") for item in events
                   if item.get("eventType") == "attempt-completed"}
           for event in events if isinstance(event, dict)):
        raise ValueError("存在未完成尝试，必须先记录结论再创建新候选")
    version_number = _next_prompt_version(run_dir)
    version = "prompt_v%d.md" % version_number
    candidate_path = _inside_run(run_dir, version, require_exists=False)
    if os.path.exists(candidate_path):
        raise ValueError("候选版本已存在，禁止覆盖：%s" % version)
    attempt_id = _next_attempt_id(events)
    shutil.copy2(parent_path, candidate_path)
    copied_tools = ""
    try:
        if parent_tools:
            tools_path = _inside_run(run_dir, parent_tools)
            copied_tools = "tools_v%d.json" % version_number
            target_tools = _inside_run(run_dir, copied_tools, require_exists=False)
            if os.path.exists(target_tools):
                raise ValueError("工具候选版本已存在，禁止覆盖：%s" % copied_tools)
            shutil.copy2(tools_path, target_tools)
        event = {
            "schemaVersion": 2,
            "eventType": "attempt-started",
            "attemptId": attempt_id,
            "createdAt": _now(),
            "version": version,
            "parentVersion": os.path.basename(parent_path),
            "toolsVersion": copied_tools,
            "rootCause": plan["rootCauseHypothesis"],
            "changeSummary": str(change_summary).strip(),
            "changeScope": plan.get("changeScope") or [],
            "requirementIntegration": plan.get("requirementIntegration") or {},
            "problemCaseIds": plan.get("problemCaseIds") or [],
            "protectedCaseIds": plan.get("neighborPassCaseIds") or [],
            "architectureTrack": plan.get("architectureTrack"),
            "planPath": os.path.relpath(plan_path, run_dir).replace("\\", "/"),
        }
        events.append(event)
        _write_atomic(_events_path(run_dir), events)
    except BaseException:
        if os.path.exists(candidate_path):
            os.unlink(candidate_path)
        if copied_tools:
            target_tools = os.path.join(run_dir, copied_tools)
            if os.path.exists(target_tools):
                os.unlink(target_tools)
        raise
    state = _load_state(run_dir)
    state.update({
        "status": "IN_PROGRESS",
        "currentBestPrompt": state.get("currentBestPrompt") or os.path.basename(parent_path),
        "currentCandidate": version,
        "activeAttemptId": attempt_id,
        "newEvidenceAvailable": False,
        "nextAction": "修改并验证 %s" % version,
        "lastIterationSummary": "已创建候选 %s：%s" % (version, change_summary),
        "updatedAt": _now(),
    })
    _write_atomic(_state_path(run_dir), state)
    render_summary(run_dir)
    return {"attemptId": attempt_id, "version": version, "toolsVersion": copied_tools}


def complete_attempt(run_dir, attempt_id, decision, conclusion, next_action,
                     round_name=None, evidence=None):
    """追加尝试结论，更新恢复状态并重新生成摘要。"""
    run_dir = os.path.abspath(run_dir)
    if decision not in VALID_DECISIONS:
        raise ValueError("decision 无效：%s" % decision)
    if not str(conclusion or "").strip() or not str(next_action or "").strip():
        raise ValueError("conclusion 和 nextAction 不能为空")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("evidence 必须为对象")
    events = _load_events(run_dir)
    starts = [event for event in events if isinstance(event, dict)
              and event.get("eventType") == "attempt-started"
              and event.get("attemptId") == attempt_id]
    if len(starts) != 1:
        raise ValueError("找不到唯一的 attempt-started：%s" % attempt_id)
    if any(event.get("eventType") == "attempt-completed" and event.get("attemptId") == attempt_id
           for event in events if isinstance(event, dict)):
        raise ValueError("尝试已经完成，禁止重复记录：%s" % attempt_id)
    start = starts[0]
    prompt_path = _inside_run(run_dir, start["version"])
    parent_path = _inside_run(run_dir, start["parentVersion"])
    manifest_path = os.path.join(run_dir, "run_manifest.json")
    manifest = None
    if decision == "accepted" and os.path.isfile(manifest_path):
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            raise ValueError("run_manifest.json 必须为对象")
    event = {
        "schemaVersion": 2,
        "eventType": "attempt-completed",
        "attemptId": attempt_id,
        "recordedAt": _now(),
        "round": round_name or "",
        "version": start["version"],
        "parentVersion": start["parentVersion"],
        "toolsVersion": start.get("toolsVersion") or "",
        "rootCause": start.get("rootCause") or "",
        "changeSummary": start.get("changeSummary") or "",
        "changeScope": start.get("changeScope") or [],
        "requirementIntegration": start.get("requirementIntegration") or {},
        "problemCaseIds": start.get("problemCaseIds") or [],
        "protectedCaseIds": start.get("protectedCaseIds") or [],
        "decision": decision,
        "reason": str(conclusion).strip(),
        "conclusion": str(conclusion).strip(),
        "nextAction": str(next_action).strip(),
        "promptSha256": _sha256(prompt_path),
        "parentPromptSha256": _sha256(parent_path),
        "promptChanged": _sha256(prompt_path) != _sha256(parent_path),
        "evidence": evidence or {},
    }
    events.append(event)
    _write_atomic(_events_path(run_dir), events)
    state = _load_state(run_dir)
    state.update({
        "status": STATE_BY_DECISION[decision],
        "currentCandidate": start["version"],
        "activeAttemptId": "",
        "lastCompletedAttemptId": attempt_id,
        "newEvidenceAvailable": True,
        "nextAction": str(next_action).strip(),
        "lastIterationSummary": str(conclusion).strip(),
        "updatedAt": _now(),
    })
    if decision == "accepted":
        state["currentBestPrompt"] = start["version"]
        if manifest is not None:
            manifest["finalPrompt"] = start["version"]
            if start.get("toolsVersion"):
                manifest["finalTools"] = start["toolsVersion"]
            manifest["decisionLog"] = "rounds.json"
            _write_atomic(manifest_path, manifest)
    state["verifiedConclusions"].append({
        "attemptId": attempt_id, "decision": decision,
        "conclusion": str(conclusion).strip(),
    })
    if decision == "rolled_back":
        state["rejectedDirections"].append({
            "attemptId": attempt_id,
            "direction": start.get("changeSummary") or start.get("rootCause") or "",
            "reason": str(conclusion).strip(),
        })
    _write_atomic(_state_path(run_dir), state)
    render_summary(run_dir)
    return event


def record_milestone(run_dir, milestone_type, conclusion, next_action, status=None):
    """记录基线重建、用例修正、平台期、暂停或交接等非提示词事件。"""
    run_dir = os.path.abspath(run_dir)
    if not str(milestone_type or "").strip() or not str(conclusion or "").strip():
        raise ValueError("milestoneType 和 conclusion 不能为空")
    if status and status not in VALID_STATUSES:
        raise ValueError("status 无效：%s" % status)
    events = _load_events(run_dir)
    event = {
        "schemaVersion": 2,
        "eventType": "milestone",
        "milestoneType": str(milestone_type).strip(),
        "recordedAt": _now(),
        "conclusion": str(conclusion).strip(),
        "nextAction": str(next_action or "").strip(),
    }
    events.append(event)
    _write_atomic(_events_path(run_dir), events)
    state = _load_state(run_dir)
    if status:
        state["status"] = status
    state.update({
        "newEvidenceAvailable": True,
        "nextAction": str(next_action or "").strip(),
        "lastIterationSummary": str(conclusion).strip(),
        "updatedAt": _now(),
    })
    _write_atomic(_state_path(run_dir), state)
    render_summary(run_dir)
    return event


def _evidence_arg(value):
    if not value:
        return {}
    if os.path.isfile(value):
        return _read_json(value, {})
    return json.loads(value)


def main():
    parser = argparse.ArgumentParser(description="管理提示词版本、实验账本和恢复摘要")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="复制父版本并创建新的 prompt_vN.md")
    start.add_argument("--run", required=True)
    start.add_argument("--parent", required=True, help="运行目录内的 prompt_vN.md")
    start.add_argument("--plan", required=True, help="运行目录内的 optimization_iteration.json")
    start.add_argument("--change-summary", required=True)
    start.add_argument("--parent-tools", default=None, help="可选：复制为同编号 tools_vN.json")
    complete = subparsers.add_parser("complete", help="记录候选结论并更新恢复摘要")
    complete.add_argument("--run", required=True)
    complete.add_argument("--attempt-id", required=True)
    complete.add_argument("--decision", choices=sorted(VALID_DECISIONS), required=True)
    complete.add_argument("--conclusion", required=True)
    complete.add_argument("--next-action", required=True)
    complete.add_argument("--round", default=None)
    complete.add_argument("--evidence", default=None, help="JSON 字符串或 JSON 文件")
    milestone = subparsers.add_parser("milestone", help="记录非提示词进展并更新摘要")
    milestone.add_argument("--run", required=True)
    milestone.add_argument("--type", required=True)
    milestone.add_argument("--conclusion", required=True)
    milestone.add_argument("--next-action", default="")
    milestone.add_argument("--status", default=None)
    summary = subparsers.add_parser("summary", help="从账本和状态重新生成恢复摘要")
    summary.add_argument("--run", required=True)
    args = parser.parse_args()
    if args.command == "start":
        result = start_attempt(args.run, args.parent, args.plan, args.change_summary, args.parent_tools)
    elif args.command == "complete":
        result = complete_attempt(args.run, args.attempt_id, args.decision, args.conclusion,
                                  args.next_action, args.round, _evidence_arg(args.evidence))
    elif args.command == "milestone":
        result = record_milestone(args.run, args.type, args.conclusion, args.next_action, args.status)
    else:
        result = {"summary": render_summary(args.run)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
