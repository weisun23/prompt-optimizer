#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-optimizer 提示词改动校验：优化后强制跑一次（仅标准库）。

校验项：
  ① 占位符集合一致（不新增、不缺失）——防止改写时误删 {变量}
  ② 内容未大段丢失（长度骤减 / 段落数骤减告警）
  ③ 工具定义结构未变（函数名与参数名不可动，仅允许改描述类）
  ④ 输出 diff 摘要（新增/删除的行数与关键片段）

用法：
  python check_prompt.py --old runs/<r>/prompt_v1.md --new runs/<r>/prompt_v2.md \
      --iteration-plan runs/<r>/optimization_iteration.json \
      --capability-inventory runs/<r>/capability-inventory.json \
      --architecture-track production-candidate
  # 退出码：0 通过（可能含告警）；1 校验失败（必须打回重改）
"""
import argparse
import difflib
import json
import os
import re
import sys

import model_profiles
import profile_constraints
import capability_guard
import iteration_guard

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

PLACEHOLDER = re.compile(r'\{([^{}\s:：,，"\'()（）]{1,40})\}')


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def placeholders(text):
    return set(PLACEHOLDER.findall(text or ''))


def tool_structure(tools):
    """提取函数名与参数名（结构类，不可动）；返回 {fn: set(params)}。"""
    out = {}
    for t in (tools or []):
        fn = t.get('function') if isinstance(t, dict) and 'function' in t else t
        name = (fn or {}).get('name')
        if not name:
            continue
        params = (fn or {}).get('parameters') or {}
        props = params.get('properties') or {}
        out[name] = set(props.keys())
    return out


def shrink_authorization_errors(allow_shrink, constraint_snapshot):
    if not allow_shrink:
        return []
    if not isinstance(constraint_snapshot, dict):
        return ['--allow-shrink 必须提供用户确认的 --constraints 快照']
    errors = profile_constraints.validate_constraint_snapshot(
        constraint_snapshot, model_profiles.load_registry())
    if errors:
        return ['--allow-shrink 的约束快照无效：%s' % '；'.join(errors)]
    selected = set(constraint_snapshot.get('selectedPrincipleIds') or [])
    if 'PRINCIPLE-CONCISE' not in selected:
        return ['用户未选择“提示词精简”，禁止使用 --allow-shrink']
    return []


def integration_location_errors(plan, old_text, new_text):
    """整合计划中的权威位置必须能在旧提示词或候选中定位。"""
    integration = (plan or {}).get('requirementIntegration') or {}
    errors = []
    strategy = integration.get('strategy')
    if strategy in ('merge-existing', 'generalize-replace'):
        missing_existing = [location for location in integration.get('existingRuleLocations') or []
                            if location not in old_text]
        if missing_existing:
            errors.append('requirementIntegration.existingRuleLocations 无法在旧提示词定位：%s'
                          % '、'.join(missing_existing))
    missing_targets = [location for location in integration.get('targetLocations') or []
                       if location not in old_text and location not in new_text]
    if missing_targets:
        errors.append('requirementIntegration.targetLocations 无法在提示词定位：%s'
                      % '、'.join(missing_targets))
    return errors


def main():
    p = argparse.ArgumentParser(description='提示词/工具改动校验（优化后必跑）')
    p.add_argument('--old', required=True, help='改动前提示词文件')
    p.add_argument('--new', required=True, help='改动后提示词文件')
    p.add_argument('--old-tools', default=None, help='改动前工具定义 JSON')
    p.add_argument('--new-tools', default=None, help='改动后工具定义 JSON')
    p.add_argument('--shrink-ratio', type=float, default=None,
                   help='可选：用户明确确认的长度收缩阈值；未提供时不使用通用比例判定')
    p.add_argument('--allow-shrink', action='store_true', help='允许大幅精简（提示词精简约束启用时用）')
    p.add_argument('--constraints', default=None,
                   help='optimization_constraints.json；使用 --allow-shrink 时必填')
    p.add_argument('--iteration-plan', required=True,
                   help='schemaVersion 3 的 optimization_iteration.json；校验需求整合分析与修改位置')
    p.add_argument('--capability-inventory', required=True,
                   help='capability-inventory.json；防止压缩或重构静默删除既有能力')
    p.add_argument('--architecture-track', choices=['production-candidate', 'experimental'], required=True)
    args = p.parse_args()

    old, new = read_text(args.old), read_text(args.new)
    errors, warns = [], []
    iteration_plan = read_json(args.iteration_plan)
    errors.extend(iteration_guard.validate_plan(iteration_plan))
    errors.extend(integration_location_errors(iteration_plan, old, new))
    constraint_snapshot = read_json(args.constraints) if args.constraints else None
    errors.extend(shrink_authorization_errors(args.allow_shrink, constraint_snapshot))
    capability_inventory = read_json(args.capability_inventory)
    errors.extend(capability_guard.validate_inventory(
        capability_inventory, args.architecture_track, new))

    # ① 占位符集合一致
    po, pn = placeholders(old), placeholders(new)
    missing, added = sorted(po - pn), sorted(pn - po)
    if missing:
        errors.append('占位符缺失（必须保留）：%s' % '、'.join('{%s}' % m for m in missing))
    if added:
        errors.append('引入了新占位符（不允许）：%s' % '、'.join('{%s}' % a for a in added))

    # ② 内容未大段丢失
    ratio = (len(new) / len(old)) if old else 1.0
    if args.shrink_ratio is not None and not 0 < args.shrink_ratio <= 1:
        errors.append('--shrink-ratio 必须大于 0 且不超过 1')
    if args.shrink_ratio is not None and ratio < args.shrink_ratio:
        msg = '内容长度缩减到 %.0f%%（%d → %d 字符），疑似大段丢失' % (ratio * 100, len(old), len(new))
        (warns if args.allow_shrink else errors).append(msg)
    old_heads = [l for l in old.splitlines() if l.strip().startswith('#')]
    new_heads = [l for l in new.splitlines() if l.strip().startswith('#')]
    lost_heads = [h for h in old_heads if h not in new_heads]
    if lost_heads:
        warns.append('丢失 %d 个标题/段落：%s' % (len(lost_heads),
                                                 '；'.join(h.strip()[:30] for h in lost_heads[:3])))

    # ③ 工具结构未变（函数名/参数名不可动）
    if args.old_tools and args.new_tools:
        so, sn = tool_structure(read_json(args.old_tools)), tool_structure(read_json(args.new_tools))
        for fn in sorted(set(so) - set(sn)):
            errors.append('工具函数被删除（函数名不可动）：%s' % fn)
        for fn in sorted(set(sn) - set(so)):
            errors.append('新增了工具函数（需用户确认）：%s' % fn)
        for fn in sorted(set(so) & set(sn)):
            lost = sorted(so[fn] - sn[fn])
            new_p = sorted(sn[fn] - so[fn])
            if lost:
                errors.append('%s 参数被删除（参数名不可动）：%s' % (fn, '、'.join(lost)))
            if new_p:
                errors.append('%s 新增参数（需用户确认）：%s' % (fn, '、'.join(new_p)))

    # ④ diff 摘要
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile=os.path.basename(args.old),
                                     tofile=os.path.basename(args.new), lineterm='', n=0))
    plus = [l for l in diff if l.startswith('+') and not l.startswith('+++')]
    minus = [l for l in diff if l.startswith('-') and not l.startswith('---')]

    print('【提示词改动校验】%s → %s' % (os.path.basename(args.old), os.path.basename(args.new)))
    print('  占位符：%d 个，集合%s' % (len(pn), '一致 ✅' if not (missing or added) else '不一致 ❌'))
    print('  长度：%d → %d 字符（%.0f%%）' % (len(old), len(new), ratio * 100))
    print('  结构净变化：字符 %+d｜标题 %+d｜整合策略 %s' % (
        len(new) - len(old), len(new_heads) - len(old_heads),
        ((iteration_plan.get('requirementIntegration') or {}).get('strategy') or '未记录')))
    print('  改动：+%d 行 / -%d 行' % (len(plus), len(minus)))
    for l in minus[:5]:
        print('    - %s' % l[1:][:100])
    for l in plus[:5]:
        print('    + %s' % l[1:][:100])
    if len(plus) + len(minus) > 10:
        print('    …（共 %d 处改动）' % (len(plus) + len(minus)))

    for w in warns:
        print('  ⚠️ %s' % w)
    for e in errors:
        print('  ❌ %s' % e)

    if errors:
        print('\n校验未通过：请修正上述问题后重新提交（占位符与工具结构不可动）')
        sys.exit(1)
    print('\n校验通过%s' % ('（含 %d 条告警，请确认是否符合预期）' % len(warns) if warns else ''))


if __name__ == '__main__':
    main()
