#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按明确 ID 顺序选择用例，供问题集、边界集和哨兵集复用。"""

import argparse
import json


def select_cases(cases, case_ids):
    """按请求顺序返回用例；缺失或重复 ID 直接失败。"""
    rows = [row for row in cases if isinstance(row, dict) and row.get("id")]
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("source 包含重复用例 ID")
    requested = list(case_ids or [])
    if len(requested) != len(set(requested)):
        raise ValueError("请求的用例 ID 重复")
    missing = [case_id for case_id in requested if case_id not in by_id]
    if missing:
        raise ValueError("缺少用例 ID：%s" % "、".join(missing))
    return [by_id[case_id] for case_id in requested]


def select_by_taxonomy(cases, filters):
    """按任务动态维度筛选；单值精确匹配，数组表示允许任一值。"""
    if not isinstance(filters, dict) or not filters:
        raise ValueError("taxonomy filters 必须为非空对象")
    selected = []
    for row in cases:
        if not isinstance(row, dict):
            continue
        values = row.get("taxonomyValues") or {}
        matched = True
        for dimension, expected in filters.items():
            allowed = expected if isinstance(expected, list) else [expected]
            actual = values.get(dimension)
            actual_values = actual if isinstance(actual, list) else [actual]
            if not set(actual_values) & set(allowed):
                matched = False
                break
        if matched:
            selected.append(row)
    return selected


def main():
    parser = argparse.ArgumentParser(description="按 ID 选择问题集、边界集或能力哨兵用例")
    parser.add_argument("--source", required=True)
    parser.add_argument("--ids", default=None, help="可选：JSON 字符串数组或包含 caseIds 的 JSON 文件")
    parser.add_argument("--where", default=None,
                        help="可选：taxonomyValues 筛选 JSON；与 --ids 选集取交集")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.source, encoding="utf-8") as stream:
        source = json.load(stream)
    cases = source if isinstance(source, list) else source.get("cases") or []
    if not args.ids and not args.where:
        parser.error("--ids 与 --where 至少提供一个")
    if args.ids:
        try:
            case_ids = json.loads(args.ids)
        except json.JSONDecodeError:
            with open(args.ids, encoding="utf-8") as stream:
                id_payload = json.load(stream)
            case_ids = id_payload if isinstance(id_payload, list) else id_payload.get("caseIds") or []
        selected = select_cases(cases, case_ids)
    else:
        selected = cases
    if args.where:
        try:
            filters = json.loads(args.where)
        except json.JSONDecodeError:
            with open(args.where, encoding="utf-8") as stream:
                filters = json.load(stream)
        selected = select_by_taxonomy(selected, filters)
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(selected, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("已选择 %d 条用例：%s" % (len(selected), args.out))


if __name__ == "__main__":
    main()
