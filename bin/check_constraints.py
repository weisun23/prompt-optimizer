#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对用户确认的提示词硬约束执行失败关闭验证。"""

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone

import llm
import model_profiles
import profile_constraints


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_PATH = os.path.join(ROOT, "prompts", "constraint_check.md")
VALID_VERDICTS = ("PASS", "FAIL", "UNVERIFIABLE")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def _canonical_hash(value):
    return _sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8"))


def _parse_json(text):
    content = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.S | re.I)
    if fenced:
        content = fenced.group(1)
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("Judge 未返回 JSON 对象")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Judge 结果不是 JSON 对象")
    return data


def combine_results(static_errors, semantic_rows):
    rows = list(semantic_rows or [])
    failed = [row for row in rows if row.get("verdict") != "PASS"]
    return {
        "verdict": "FAIL" if static_errors or failed else "PASS",
        "staticErrors": list(static_errors or []),
        "results": rows,
    }


def _load_check_system_prompt():
    with open(PROMPT_PATH, encoding="utf-8") as stream:
        return stream.read()


def _render_check_payload(snapshot, constraint, prompt_text, optimizer_evidence):
    return json.dumps({
        "profileLabel": snapshot.get("profileLabel") or snapshot.get("profileId") or "generic",
        "constraintId": constraint.get("id") or "",
        "constraintText": constraint.get("text") or "",
        "optimizerEvidence": optimizer_evidence or "（未提供）",
        "candidatePrompt": prompt_text,
    }, ensure_ascii=False, indent=2)


def validate_candidate(config, snapshot, prompt_text, compliance, timeout=180,
                       registry=None):
    registry = registry or model_profiles.load_registry()
    static_errors = profile_constraints.validate_constraint_snapshot(snapshot, registry)
    static_errors.extend(profile_constraints.validate_compliance(snapshot, compliance))
    result = combine_results(static_errors, [])
    result.update({
        "schemaVersion": 1,
        "profileId": snapshot.get("profileId"),
        "promptSha256": _sha256_bytes(str(prompt_text).encode("utf-8")),
        "constraintsSha256": _canonical_hash(snapshot),
        "validatedAt": _now(),
        "judge": {
            "model": (((config.get("models") or {}).get("judge") or {}).get("model")),
            "provider": (((config.get("models") or {}).get("judge") or {}).get("provider")),
        },
    })
    if static_errors:
        return result

    compliance_by_id = {row.get("id"): row for row in compliance or [] if isinstance(row, dict)}
    semantic_rows = []
    for constraint in snapshot.get("hardConstraints") or []:
        constraint_id = constraint.get("id")
        try:
            check_payload = _render_check_payload(
                snapshot, constraint, prompt_text,
                (compliance_by_id.get(constraint_id) or {}).get("evidence"))
            reply = llm.chat(config, "judge", [
                {"role": "system", "content": _load_check_system_prompt()},
                {"role": "user", "content": check_payload},
            ],
                             temperature=0, reasoning=False, timeout=timeout)
            data = _parse_json(reply.get("content"))
            verdict = str(data.get("verdict") or "").upper()
            evidence = str(data.get("evidence") or "").strip()
            if verdict not in VALID_VERDICTS or (verdict == "PASS" and not evidence):
                raise ValueError("Judge 返回的 verdict 或 evidence 无效")
            semantic_rows.append({
                "id": constraint_id,
                "verdict": verdict,
                "evidence": evidence,
                "reason": str(data.get("reason") or "").strip(),
            })
        except Exception as error:
            semantic_rows.append({
                "id": constraint_id,
                "verdict": "UNVERIFIABLE",
                "evidence": "",
                "reason": str(error),
            })

    combined = combine_results([], semantic_rows)
    result["verdict"] = combined["verdict"]
    result["results"] = combined["results"]
    return result


def _read_text(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def _read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description="验证用户确认的提示词硬约束")
    parser.add_argument("--config", required=True)
    parser.add_argument("--constraints", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--compliance", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    result = validate_candidate(
        _read_json(args.config), _read_json(args.constraints), _read_text(args.prompt),
        _read_json(args.compliance), timeout=args.timeout)
    profile_constraints.write_json_atomic(args.out, result)
    print("硬约束验证：%s（%s）" % (result["verdict"], args.out))
    if result["verdict"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
