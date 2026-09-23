#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确保基线候选在同一生产模式、模型配置和用例集下比较。"""

import argparse
import json
import re


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_selection(selection):
    """校验基线选择证据，禁止按最新文件编号猜测最佳版本。"""
    if not isinstance(selection, dict) or selection.get("schemaVersion") != 1:
        return ["baseline-selection.json schemaVersion 必须为 1"]
    rows = selection.get("candidates")
    if not isinstance(rows, list) or len(rows) < 1:
        return ["candidates 不能为空"]
    errors, ids = [], set()
    scopes = {"modelConfigSha256": set(), "caseSetSha256": set(), "productionMode": set()}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            errors.append("候选缺少 id")
            continue
        if row["id"] in ids:
            errors.append("候选 ID 重复：%s" % row["id"])
        ids.add(row["id"])
        for key in ("promptSha256", "modelConfigSha256", "caseSetSha256"):
            value = str(row.get(key) or "")
            if not SHA256.fullmatch(value):
                errors.append("候选 %s 的 %s 无效" % (row["id"], key))
        for key in scopes:
            scopes[key].add(row.get(key))
        if not str(row.get("productionMode") or "").strip():
            errors.append("候选 %s 缺少 productionMode" % row["id"])
    if selection.get("selectedId") not in ids:
        errors.append("selectedId 不在 candidates 中")
    if not str(selection.get("selectionReason") or "").strip():
        errors.append("selectionReason 不能为空")
    for key, values in scopes.items():
        if len(values) != 1:
            errors.append("候选未使用相同 %s" % key)
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验提示词基线选择")
    parser.add_argument("--selection", required=True)
    args = parser.parse_args()
    with open(args.selection, encoding="utf-8") as stream:
        selection = json.load(stream)
    errors = validate_selection(selection)
    if errors:
        raise SystemExit("基线选择门禁失败：%s" % "；".join(errors))
    print("基线选择门禁通过：%s" % selection["selectedId"])


if __name__ == "__main__":
    main()
