#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-optimizer 轮次对比：生成跨轮次的同口径证据（仅标准库）。

功能：
  ① 总体对比：均分 / 通过率 / 失败数 / 根因分布变化
  ② 分类对比：每个场景分类的通过率与均分变化（定位哪类改好了、哪类退化了）
  ③ 用例级变化：修复（FAIL→PASS）、退化（PASS→FAIL）清单
  ④ 不使用通用修复率或噪声阈值自动接受/回退；最终判定交给任务级接受策略
  ⑤ 写入 runs/<run>/rounds.json（机器可读的轮次证据与人工决策日志）

用法：
  python compare_rounds.py --run runs/<r> --rounds round1,round2 [--task tasks/<t>/task.json]
  python compare_rounds.py --run runs/<r>            # 自动取最后两轮
  python compare_rounds.py --run runs/<r> --record --decision accepted --reason "任务级接受策略通过"
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import score_protocol

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def load_round(run_dir, rnd):
    """读一轮分数：{case_id: score}"""
    rdir = os.path.join(run_dir, 'scores', rnd)
    return score_protocol.load_score_round(rdir)[0]


def list_rounds(run_dir):
    root = os.path.join(run_dir, 'scores')
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def stats(scores):
    normalized = {case_id: score_protocol.normalize_score(score)
                  for case_id, score in scores.items()}
    valid = {case_id: score for case_id, score in normalized.items()
             if score.get('verdict') in ('PASS', 'FAIL')
             and isinstance(score.get('totalScore'), (int, float))}
    invalid = [case_id for case_id, score in normalized.items()
               if score.get('verdict') == 'INVALID']
    vals = [score['totalScore'] for score in valid.values()]
    fails = [case_id for case_id, score in valid.items() if score.get('verdict') == 'FAIL']
    labels = {}
    for c in fails:
        lb = valid[c].get('failLabel', '?')
        labels[lb] = labels.get(lb, 0) + 1
    return {
        'count': len(valid),
        'totalCount': len(normalized),
        'invalidCount': len(invalid),
        'avg': round(sum(vals) / len(vals), 1) if vals else 0,
        'fails': fails,
        'passRate': round(100.0 * (len(valid) - len(fails)) / max(1, len(valid)), 1),
        'labels': labels,
    }


def by_category(scores):
    cats = {}
    for cid, raw in scores.items():
        s = score_protocol.normalize_score(raw)
        if s.get('verdict') == 'INVALID' or not isinstance(s.get('totalScore'), (int, float)):
            continue
        cat = s.get('category') or '（未分类）'
        d = cats.setdefault(cat, {'n': 0, 'pass': 0, 'sum': 0.0})
        d['n'] += 1
        if s.get('verdict') != 'FAIL':
            d['pass'] += 1
        if isinstance(s.get('totalScore'), (int, float)):
            d['sum'] += s['totalScore']
    for d in cats.values():
        d['avg'] = round(d['sum'] / max(1, d['n']), 1)
        d['rate'] = round(100.0 * d['pass'] / max(1, d['n']), 1)
    return cats


def align_case_sets(base, current, allow_subset=False):
    """默认要求同一用例集合；显式子集模式只比较交集并返回范围说明。"""
    base = {case_id: score for case_id, score in base.items()
            if score_protocol.normalize_score(score).get('verdict') in ('PASS', 'FAIL')
            and isinstance(score_protocol.normalize_score(score).get('totalScore'), (int, float))}
    current = {case_id: score for case_id, score in current.items()
               if score_protocol.normalize_score(score).get('verdict') in ('PASS', 'FAIL')
               and isinstance(score_protocol.normalize_score(score).get('totalScore'), (int, float))}
    base_ids, current_ids = set(base), set(current)
    scope = {
        'baseCount': len(base_ids), 'currentCount': len(current_ids),
        'intersectionCount': len(base_ids & current_ids),
        'isSubset': base_ids != current_ids,
    }
    if base_ids == current_ids:
        return dict(base), dict(current), scope
    if not allow_subset:
        raise ValueError(
            '用例集合不一致：基线 %d 条，当前 %d 条，交集 %d 条；'
            '如确需比较回归子集，请显式添加 --allow-subset'
            % (scope['baseCount'], scope['currentCount'], scope['intersectionCount']))
    common = sorted(base_ids & current_ids)
    if not common:
        raise ValueError('用例集合不一致且没有交集，无法比较')
    return ({case_id: base[case_id] for case_id in common},
            {case_id: current[case_id] for case_id in common}, scope)


def arrow(delta):
    if delta == 0:
        return '≈'
    return '↑' if delta > 0 else '↓'


def main():
    p = argparse.ArgumentParser(description='轮次同口径证据对比；不自动接受或回退')
    p.add_argument('--run', required=True, help='runs/<run> 目录')
    p.add_argument('--rounds', default=None, help='对比的两轮（如 round1,round2）；缺省取最后两轮')
    p.add_argument('--task', default=None, help='兼容旧命令保留；接受标准由 iteration_guard 单独验证')
    p.add_argument('--record', action='store_true', help='把本轮结果写入 rounds.json')
    p.add_argument('--version', default=None, help='本轮提示词版本（如 prompt_v2.md），配合 --record')
    p.add_argument('--decision', default=None, choices=['accepted', 'rolled_back'], help='人工决策，配合 --record')
    p.add_argument('--reason', default='', help='决策理由，配合 --record')
    p.add_argument('--allow-subset', action='store_true',
                   help='允许两轮用例集合不同；只比较交集并明确标注子集范围')
    args = p.parse_args()

    rounds = list_rounds(args.run)
    if not rounds:
        sys.exit('未找到分数目录：%s/scores/<round>/' % args.run)
    if args.rounds:
        picks = [r.strip() for r in args.rounds.split(',') if r.strip()]
    else:
        picks = rounds[-2:] if len(rounds) >= 2 else rounds
    if len(picks) == 1:
        base, cur = None, load_round(args.run, picks[0])
        base_name, cur_name = '（无基线）', picks[0]
    else:
        base, cur = load_round(args.run, picks[0]), load_round(args.run, picks[-1])
        base_name, cur_name = picks[0], picks[-1]

    scope = None
    if base:
        try:
            base, cur, scope = align_case_sets(base, cur, args.allow_subset)
        except ValueError as error:
            sys.exit(str(error))

    cs = stats(cur)
    print('【轮次对比】%s → %s' % (base_name, cur_name))
    if scope and scope['isSubset']:
        print('  ⚠️ 子集比较：基线 %d 条｜当前 %d 条｜仅比较交集 %d 条；以下均分和通过率仅代表交集'
              % (scope['baseCount'], scope['currentCount'], scope['intersectionCount']))
    if not base:
        print('  用例 %d 条｜均分 %s｜通过率 %s%%｜失败 %d 条'
              % (cs['count'], cs['avg'], cs['passRate'], len(cs['fails'])))
        if cs['labels']:
            print('  根因分布：%s' % json.dumps(cs['labels'], ensure_ascii=False))
        return

    bs = stats(base)
    d_avg, d_rate = round(cs['avg'] - bs['avg'], 1), round(cs['passRate'] - bs['passRate'], 1)
    print('  均分：%s → %s（%s%+.1f）' % (bs['avg'], cs['avg'], arrow(d_avg), d_avg))
    print('  通过率：%s%% → %s%%（%s%+.1f）'
          % (bs['passRate'], cs['passRate'], arrow(d_rate), d_rate))
    print('  失败数：%d → %d' % (len(bs['fails']), len(cs['fails'])))
    print('  根因：%s → %s' % (json.dumps(bs['labels'], ensure_ascii=False),
                              json.dumps(cs['labels'], ensure_ascii=False)))

    # 用例级变化
    fixed = [c for c in bs['fails'] if score_protocol.normalize_score(cur[c]).get('verdict') == 'PASS']
    broke = [c for c, s in cur.items()
             if score_protocol.normalize_score(s).get('verdict') == 'FAIL'
             and score_protocol.normalize_score(base[c]).get('verdict') == 'PASS']
    still = [c for c in bs['fails'] if score_protocol.normalize_score(cur[c]).get('verdict') == 'FAIL']
    fix_rate = round(len(fixed) / max(1, len(bs['fails'])), 3)
    print('\n  修复（FAIL→PASS）%d 条：%s' % (len(fixed), '、'.join(fixed[:8]) or '无'))
    print('  退化（PASS→FAIL）%d 条：%s' % (len(broke), '、'.join(broke[:8]) or '无'))
    print('  仍失败 %d 条：%s' % (len(still), '、'.join(still[:8]) or '无'))
    print('  失败修复率：%.1f%%（仅作证据，不使用通用阈值判定）' % (fix_rate * 100))

    # 分类对比
    bc, cc = by_category(base), by_category(cur)
    print('\n  分类对比：')
    print('    %-22s %-18s %-18s' % ('分类', '通过率', '均分'))
    for cat in sorted(set(bc) | set(cc)):
        b, c = bc.get(cat), cc.get(cat)
        if not b or not c:
            print('    %-22s %s' % (cat[:22], '（仅一轮有数据）'))
            continue
        dr, da = c['rate'] - b['rate'], c['avg'] - b['avg']
        print('    %-22s %5.1f%%→%5.1f%% %s  %5.1f→%5.1f %s'
              % (cat[:22], b['rate'], c['rate'], arrow(dr), b['avg'], c['avg'], arrow(da)))

    print('\n【判定】本脚本不自动接受或回退')
    print('  请把修复、退化、分类变化和重复运行证据交给任务级 acceptance-policy.json 与 iteration_guard.py 判定。')

    # rounds.json 决策日志
    if args.record:
        path = os.path.join(args.run, 'rounds.json')
        log = read_json(path) or []
        log.append({
            'schemaVersion': 2, 'eventType': 'comparison-recorded',
            'round': cur_name, 'baseRound': base_name,
            'recordedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'version': args.version or '', 'count': cs['count'],
            'avg': cs['avg'], 'avgDelta': d_avg,
            'passRate': cs['passRate'], 'passRateDelta': d_rate,
            'fails': len(cs['fails']), 'fixed': len(fixed), 'broke': len(broke),
            'failFixRate': fix_rate, 'failLabels': cs['labels'],
            'suggestion': 'requires_iteration_guard',
            'decision': args.decision or '', 'reason': args.reason,
        })
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        print('\n已记录决策日志 → %s' % path)


if __name__ == '__main__':
    main()
