#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验任务级动态用例分类体系及其取值。"""

import argparse
import hashlib
import json


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_taxonomy(taxonomy):
    """分类维度、等级数量和含义完全由任务定义；这里只校验结构。"""
    if not isinstance(taxonomy, dict) or taxonomy.get("schemaVersion") != 1:
        return ["case-taxonomy.json schemaVersion 必须为 1"]
    errors = []
    if not _text(taxonomy.get("source")):
        errors.append("taxonomy.source 必须记录用户或正式契约来源")
    dimensions = taxonomy.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        return errors + ["taxonomy.dimensions 不能为空"]
    dimension_ids = set()
    for index, dimension in enumerate(dimensions, 1):
        if not isinstance(dimension, dict):
            errors.append("dimensions 第 %d 项必须为对象" % index)
            continue
        dimension_id = dimension.get("id")
        if not _text(dimension_id):
            errors.append("dimensions 第 %d 项缺少 id" % index)
            continue
        if dimension_id in dimension_ids:
            errors.append("维度 ID 重复：%s" % dimension_id)
        dimension_ids.add(dimension_id)
        for key in ("name", "purpose"):
            if not _text(dimension.get(key)):
                errors.append("维度 %s 缺少 %s" % (dimension_id, key))
        if dimension.get("required") not in (True, False):
            errors.append("维度 %s required 必须为布尔值" % dimension_id)
        values = dimension.get("values")
        if not isinstance(values, list) or not values:
            errors.append("维度 %s values 不能为空" % dimension_id)
            continue
        value_ids = set()
        for value in values:
            value_id = value.get("id") if isinstance(value, dict) else None
            if not _text(value_id):
                errors.append("维度 %s 存在缺少 id 的等级" % dimension_id)
                continue
            if value_id in value_ids:
                errors.append("维度 %s 的等级 ID 重复：%s" % (dimension_id, value_id))
            value_ids.add(value_id)
            for key in ("name", "assignmentRule"):
                if not _text(value.get(key)):
                    errors.append("维度 %s 的等级 %s 缺少 %s" % (dimension_id, value_id, key))
    stability = taxonomy.get("stabilityPolicy")
    if not isinstance(stability, dict):
        errors.append("stabilityPolicy 必须为对象")
    else:
        if not _text(stability.get("source")):
            errors.append("stabilityPolicy.source 必须记录口径来源")
        labels = stability.get("labels")
        if not isinstance(labels, list) or not labels:
            errors.append("stabilityPolicy.labels 不能为空")
        else:
            seen = set()
            for label in labels:
                label_id = label.get("id") if isinstance(label, dict) else None
                if not _text(label_id):
                    errors.append("stabilityPolicy 存在缺少 id 的标签")
                    continue
                if label_id in seen:
                    errors.append("稳定性标签 ID 重复：%s" % label_id)
                seen.add(label_id)
                if not _text(label.get("name")) or not _text(label.get("assignmentRule")):
                    errors.append("稳定性标签 %s 缺少 name 或 assignmentRule" % label_id)
    return errors


def dimension_map(taxonomy):
    return {dimension["id"]: dimension for dimension in taxonomy.get("dimensions") or []
            if isinstance(dimension, dict) and _text(dimension.get("id"))}


def stability_label_ids(taxonomy):
    return {label.get("id") for label in (taxonomy.get("stabilityPolicy") or {}).get("labels") or []
            if isinstance(label, dict) and _text(label.get("id"))}


def validate_values(taxonomy, values):
    """校验一个覆盖项或用例对任务级分类体系的引用。"""
    errors = validate_taxonomy(taxonomy)
    if errors:
        return errors
    if not isinstance(values, dict):
        return ["taxonomyValues 必须为对象"]
    dimensions = dimension_map(taxonomy)
    unknown = sorted(set(values) - set(dimensions))
    if unknown:
        errors.append("taxonomyValues 包含未知维度：%s" % "、".join(unknown))
    for dimension_id, dimension in dimensions.items():
        allowed = {value["id"] for value in dimension["values"]}
        selected = values.get(dimension_id)
        if selected is None:
            if dimension.get("required") is True:
                errors.append("taxonomyValues 缺少必填维度 %s，可选值：%s" % (
                    dimension_id, "、".join(sorted(allowed))))
            continue
        selected_values = selected if isinstance(selected, list) else [selected]
        if not selected_values or not all(isinstance(value, str) and value in allowed
                                          for value in selected_values):
            errors.append("taxonomyValues.%s 取值无效，可选值：%s" % (
                dimension_id, "、".join(sorted(allowed))))
    return errors


def main():
    parser = argparse.ArgumentParser(description="校验任务级动态用例分类体系")
    parser.add_argument("--taxonomy", required=True)
    parser.add_argument("--values", default=None, help="可选：taxonomyValues JSON 文件")
    args = parser.parse_args()
    with open(args.taxonomy, encoding="utf-8") as stream:
        taxonomy = json.load(stream)
    errors = validate_taxonomy(taxonomy)
    if args.values:
        with open(args.values, encoding="utf-8") as stream:
            errors.extend(validate_values(taxonomy, json.load(stream)))
    if errors:
        raise SystemExit("分类体系校验失败：%s" % "；".join(errors))
    with open(args.taxonomy, "rb") as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    print(json.dumps({"verdict": "PASS", "sha256": digest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
