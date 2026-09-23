#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防止提示词压缩或重构时静默丢失既有业务能力。"""

import argparse
import json


VALID_STATUSES = frozenset(("preserved", "modified-authorized", "deferred-experimental", "removed-authorized"))


def validate_inventory(inventory, architecture_track, candidate_text):
    """校验能力登记与候选文本；生产候选不得延后必需能力。"""
    if not isinstance(inventory, dict) or inventory.get("schemaVersion") != 1:
        return ["capability-inventory.json schemaVersion 必须为 1"]
    items = inventory.get("items")
    if not isinstance(items, list) or not items:
        return ["capability inventory items 不能为空"]
    errors, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            errors.append("能力条目缺少 id")
            continue
        item_id = item["id"]
        if item_id in seen:
            errors.append("能力 ID 重复：%s" % item_id)
        seen.add(item_id)
        status = item.get("status")
        if status not in VALID_STATUSES:
            errors.append("能力 %s status 无效" % item_id)
            continue
        if architecture_track == "production-candidate" and item.get("required") is True \
                and status not in ("preserved", "modified-authorized"):
            errors.append("生产候选缺少必需能力：%s" % item_id)
        markers = item.get("candidateMarkers")
        if status in ("preserved", "modified-authorized"):
            if not isinstance(markers, list) or not markers:
                errors.append("能力 %s 缺少 candidateMarkers" % item_id)
            else:
                missing = [marker for marker in markers
                           if not isinstance(marker, str) or marker not in candidate_text]
                if missing:
                    errors.append("能力 %s 的候选证据不存在：%s" % (
                        item_id, "、".join(map(str, missing))))
        if status in ("modified-authorized", "removed-authorized") \
                and not str(item.get("authorization") or "").strip():
            errors.append("能力 %s 的修改或删除缺少用户授权" % item_id)
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验候选提示词的能力覆盖清单")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--track", choices=["production-candidate", "experimental"], required=True)
    args = parser.parse_args()
    with open(args.inventory, encoding="utf-8") as stream:
        inventory = json.load(stream)
    with open(args.candidate, encoding="utf-8") as stream:
        candidate = stream.read()
    errors = validate_inventory(inventory, args.track, candidate)
    if errors:
        raise SystemExit("能力覆盖门禁失败：%s" % "；".join(errors))
    print("能力覆盖门禁通过")


if __name__ == "__main__":
    main()
