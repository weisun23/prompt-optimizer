# -*- coding: utf-8 -*-
"""评分数据协议兼容层。

新写入统一使用 totalScore/verdict/scoreMode；读取时兼容旧版工具二值评分的 score 字段。
"""
import json
import os


VALID_VERDICTS = ("PASS", "FAIL", "INVALID")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_score(row):
    """返回统一评分对象，不修改调用方传入的字典。"""
    out = dict(row or {})
    verdict = str(out.get("verdict") or "").upper()
    total = out.get("totalScore")

    if verdict == "INVALID":
        out["verdict"] = "INVALID"
        out["totalScore"] = total if _number(total) else None
        out.setdefault("scoreMode", "rubric")
        out.setdefault("failLabel", "device_error")
        return out

    legacy_binary = not _number(total) and _number(out.get("score"))
    if legacy_binary:
        total = out["score"]
        out.setdefault("scoreMode", "tool-binary")
    elif _number(total):
        mode = out.get("evalMode")
        out.setdefault("scoreMode", mode if mode in ("expected", "expected-semantic") else "rubric")
    else:
        total = None

    out["totalScore"] = total
    if verdict not in VALID_VERDICTS:
        if legacy_binary:
            verdict = "PASS" if total == 100 else "FAIL"
        else:
            verdict = "INVALID"
            out.setdefault("failLabel", "score_contract_error")
            out.setdefault("reason", "评分记录缺少 verdict，不能使用通用分数阈值猜测")
    out["verdict"] = verdict
    if verdict == "PASS":
        out.setdefault("failLabel", "none")
    elif out.get("scoreMode") == "tool-binary":
        out.setdefault("failLabel", "tool_misuse")
    return out


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def load_score_round(round_dir):
    """读取一轮分数并单独返回摘要；任何下划线前缀 JSON 都不作为用例。"""
    items = {}
    summary = None
    if not os.path.isdir(round_dir):
        return items, summary
    for filename in sorted(os.listdir(round_dir)):
        if not filename.endswith(".json"):
            continue
        data = read_json(os.path.join(round_dir, filename))
        if filename == "_summary.json":
            summary = data
            continue
        if filename.startswith("_") or not isinstance(data, dict):
            continue
        case_id = data.get("caseId") or filename[:-5]
        normalized = normalize_score(data)
        normalized["caseId"] = case_id
        items[case_id] = normalized
    return items, summary


def summarize_scores(items):
    rows = [normalize_score(row) for row in (items or {}).values() if isinstance(row, dict)]
    valid = [row for row in rows if row.get("verdict") in ("PASS", "FAIL") and _number(row.get("totalScore"))]
    passed = [row for row in valid if row.get("verdict") == "PASS"]
    invalid = [row for row in rows if row.get("verdict") == "INVALID"]
    avg = round(sum(row["totalScore"] for row in valid) / len(valid), 1) if valid else 0
    pass_rate = round(100.0 * len(passed) / len(valid), 1) if valid else 0
    return {
        "count": len(rows),
        "scoredCount": len(valid),
        "invalidCount": len(invalid),
        "correctCount": len(passed),
        "wrongCount": len(valid) - len(passed),
        "avg": avg,
        "score": avg,
        "passRate": pass_rate,
    }


def make_binary_score(case_id, score, reason, errors=None, judge_called=False, **extra):
    value = 100 if float(score) >= 100 else 0
    row = {
        "caseId": case_id,
        "scoreMode": "tool-binary",
        "score": value,
        "totalScore": value,
        "verdict": "PASS" if value == 100 else "FAIL",
        "failLabel": "none" if value == 100 else "tool_misuse",
        "reason": reason or ("函数调用完全正确" if value == 100 else "函数调用不满足二值满分条件"),
        "errors": list(errors or []),
        "judgeCalled": bool(judge_called),
    }
    row.update(extra)
    return row
