#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-optimizer 评分器：对轨迹打分（仅标准库）。

职责（确定性逻辑）：
  拼评分提示词（**所有占位符强制填充**）/ dimensionsText 权重归一化 /
  工具调用对照（native arguments 或 text-protocol 提槽参数 vs expectedToolArgs）/
  多次评分取平均 + 分歧重评 / JSON 容错 / regress.json 自动写入 / 并发 / 汇总

用法：
  python scorer.py --config config.json --task tasks/<t>/task.json \
      --trajectories runs/<r>/trajectories --cases cases/generated \
      --out runs/<r>/scores/round1 --regress cases/regress.json [--concurrency 2]
"""
import argparse
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402
import evaluation_mode  # noqa: E402
import run_manifest  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 提示词目录：优先脚本同级的 ../prompts（工作区副本），可用 --prompts-dir 覆盖
PROMPTS_DIR = os.path.join(_BASE, 'prompts')


def load_trajectories(directory):
    """读取用例轨迹，排除 _run_info.json 等元数据文件。"""
    rows = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith('.json') or filename.startswith('_'):
            continue
        data = read_json(os.path.join(directory, filename))
        if isinstance(data, dict) and data.get('caseId'):
            rows.append(data)
    return rows
# failLabel 白名单：judge 模式 5 个 + expected 模式 3 个 + 装置类 1 个
LABELS_JUDGE = ('flow_break', 'info_error', 'reply_deviation', 'tool_misuse', 'none')
LABELS_EXPECTED = ('result_mismatch', 'format_error', 'missing_content', 'info_error', 'none')
DEFAULT_DIMS = [
    {'name': 'result', 'standard': '结果正确性：最终输出是否准确、是否达成用户目标、与输入是否一致', 'weight': 0.4},
    {'name': 'process', 'standard': '流程完整性：关键信息是否收集齐全、该确认的是否确认、步骤是否完整有序', 'weight': 0.3},
    {'name': 'reply', 'standard': '回复质量：是否贴合用户需求、有无答非所问、信息是否完整、表达是否清晰', 'weight': 0.3},
]
_lock = threading.Lock()  # 数据锁（results/idx）
_print_lock = threading.Lock()  # 日志锁（独立：Lock 不可重入，数据锁内调 log 会死锁）


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_cases_map(path):
    cases = {}
    files = []
    if path and os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith('.json')]
    elif path and os.path.isfile(path):
        files = [path]
    for fp in files:
        data = read_json(fp)
        for c in (data if isinstance(data, list) else [data]):
            if c.get('id'):
                cases[c['id']] = c
    return cases


def dimensions_text(task):
    """渲染维度文本：**权重归一化到合计 1**（避免 judge 直接相加超过 100）。"""
    dims = task.get('dimensions') or DEFAULT_DIMS
    weights = []
    for d in dims:
        w = d.get('weight')
        weights.append(float(w) if isinstance(w, (int, float)) and w > 0 else 1.0)
    total = sum(weights) or 1.0
    lines = []
    for d, w in zip(dims, weights):
        lines.append('- %s：%s（权重 %.2f）' % (d.get('name'), d.get('standard') or '', w / total))
    return '\n'.join(lines), [d.get('name') for d in dims], [w / total for w in weights]


def traj_io(traj):
    """从轨迹取评分用的 input/output 文本。"""
    if traj.get('input') is not None and traj.get('output') is not None:
        return traj['input'], traj['output']
    lines = []
    for m in traj.get('messages') or []:
        role = {'user': '使用者', 'assistant': '被测模型', 'tool': '工具返回'}.get(m.get('role'), m.get('role'))
        txt = m.get('content') or ''
        if m.get('tool_calls'):
            txt += ''.join('\n[调用工具] %s(%s)' % (t.get('name'), t.get('arguments'))
                           for t in m['tool_calls'])
        lines.append('%s: %s' % (role, txt))
    convo = '\n'.join(lines)
    meta = '场景：%s\n结束原因：%s（%s/%s 轮）' % (
        traj.get('scenario') or '', traj.get('endReason'), traj.get('turns'), traj.get('maxTurns'))
    facts = traj.get('variables') or {}
    if facts:
        meta += ('\n运行时事实快照（来自 system 注入，评分时视为已知且可信的事实；'
                 '被测模型可直接引用，无需工具返回）：\n%s'
                 % json.dumps(facts, ensure_ascii=False, indent=2))
    return meta, convo


def actual_tool_calls(traj):
    """实际工具调用：native=tool_calls arguments；text-protocol=提槽参数（runner 已统一写入 toolCalls）。"""
    out = []
    for t in traj.get('toolCalls') or []:
        args = t.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        out.append({'name': t.get('name'), 'arguments': args or {}})
    for m in traj.get('messages') or []:
        for t in m.get('tool_calls') or []:
            args = t.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            item = {'name': t.get('name'), 'arguments': args or {}}
            if item not in out:
                out.append(item)
    return out


def _tool_schemas(task):
    out = {}
    for tool in (task or {}).get('tools') or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
        name = fn.get('name')
        if name:
            params = fn.get('parameters') or fn.get('input_schema') or {}
            out[name] = params if isinstance(params, dict) else {}
    return out


def _normalize_expected_args(raw):
    if isinstance(raw, dict):
        return raw
    out = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        fn_obj = item.get('function')
        fn = (fn_obj or {}).get('name') if isinstance(fn_obj, dict) else (item.get('name') or fn_obj)
        args = item.get('arguments') if item.get('arguments') is not None else item.get('args')
        if not fn:
            continue
        if fn in out:
            out[fn] = (out[fn] if isinstance(out[fn], list) else [out[fn]]) + [args or {}]
        else:
            out[fn] = args or {}
    return out


def _arg_diffs(expected, actual, ignored, semantic_fields=None):
    expected = expected if isinstance(expected, dict) else {}
    actual = actual if isinstance(actual, dict) else {'raw': actual}
    semantic_fields = set(semantic_fields or [])
    diffs = []
    semantic_notes = []
    for key, value in expected.items():
        if key not in actual:
            diffs.append('%s 缺失（期望 %s）' % (key, value))
        elif key in semantic_fields:
            if str(actual.get(key)).strip() != str(value).strip():
                semantic_notes.append(
                    '%s 期望“%s”、实际“%s”（自由文本参数交由评分模型按核心语义判断）'
                    % (key, value, actual.get(key)))
        elif str(actual.get(key)).strip() != str(value).strip():
            diffs.append('%s 不符（期望 %s，实际 %s）' % (key, value, actual.get(key)))
    extra = [key for key in actual if key not in expected and key not in ignored]
    semantic_extra = [key for key in extra if key in semantic_fields]
    deterministic_extra = [key for key in extra if key not in semantic_fields]
    if deterministic_extra:
        diffs.append('多出参数 %s' % '、'.join(deterministic_extra))
    if semantic_extra:
        semantic_notes.append('额外语义参数 %s 的值为“%s”（交由评分模型判断是否有依据且改变任务含义）' % (
            '、'.join(semantic_extra), '；'.join('%s=%s' % (key, actual.get(key)) for key in semantic_extra)))
    return diffs, semantic_notes


def tool_check(case, traj, task=None):
    """schema 感知的工具对照；支持同一函数多次调用的参数对象数组。"""
    expect_names = case.get('toolsExpect') or []
    if isinstance(expect_names, str):
        expect_names = [expect_names]
    expect_names = [n for n in expect_names if isinstance(n, str) and n.strip()]
    expect_args = _normalize_expected_args(case.get('expectedToolArgs') or {})
    if not expect_names and not expect_args:
        return '无', '无'

    exp_lines = []
    if expect_names:
        exp_lines.append('期望调用工具：%s' % '、'.join(expect_names))
    for fn, args in expect_args.items():
        exp_lines.append('期望 %s 参数：%s' % (fn, json.dumps(args, ensure_ascii=False)))

    schemas = _tool_schemas(task)
    ignored = {'callid'} | set((task or {}).get('systemToolArgs') or [])
    semantic_fields = set((task or {}).get('semanticToolArgFields') or [])
    actual = actual_tool_calls(traj)
    actual_names = [a['name'] for a in actual]
    expected_tools = set(expect_names) | set(expect_args)
    chk = []
    for name in expected_tools:
        if name not in actual_names:
            chk.append('❌ 该调未调：%s' % name)
    extra_calls = [n for n in actual_names if n not in expected_tools]
    if extra_calls:
        chk.append('ℹ️ 额外调用：%s（用例未列出；按场景判断是否合理）'
                   % '、'.join(dict.fromkeys(extra_calls)))

    for fn, expected in expect_args.items():
        acts = [a['arguments'] for a in actual if a['name'] == fn]
        if not acts:
            continue
        groups = expected if isinstance(expected, list) else [expected]
        groups = [g for g in groups if isinstance(g, dict)]
        if not groups:
            chk.append('⚠️ %s 的 expectedToolArgs 不是参数对象，测试契约无效' % fn)
            continue

        props = ((schemas.get(fn) or {}).get('properties') or {})
        invalid_keys = sorted({key for group in groups for key in group if props and key not in props})
        if invalid_keys:
            chk.append('⚠️ %s 的期望参数名不在工具 schema：%s（测试契约问题，不归因被测模型）'
                       % (fn, '、'.join(invalid_keys)))
            continue

        if len(groups) == 1:
            attempts = [(_arg_diffs(groups[0], act, ignored, semantic_fields), act) for act in acts]
            (best, semantic_notes), _ = min(attempts, key=lambda item: len(item[0][0]))
            chk.append(('✅ %s 参数一致' % fn) if not best
                       else ('❌ %s 参数差异：%s' % (fn, '；'.join(best))))
            if semantic_notes:
                chk.append('ℹ️ %s' % '；'.join(semantic_notes))
            continue

        unused = list(enumerate(acts))
        for index, group in enumerate(groups, 1):
            choices = [(_arg_diffs(group, act, ignored, semantic_fields), pos, act) for pos, act in unused]
            if not choices:
                chk.append('❌ %s 第 %d 组期望参数没有对应的实际调用' % (fn, index))
                continue
            (diffs, semantic_notes), pos, _ = min(choices, key=lambda item: len(item[0][0]))
            unused = [(p, a) for p, a in unused if p != pos]
            chk.append(('✅ %s 第 %d 组参数一致' % (fn, index)) if not diffs
                       else ('❌ %s 第 %d 组参数差异：%s' % (fn, index, '；'.join(diffs))))
            if semantic_notes:
                chk.append('ℹ️ %s 第 %d 组：%s' % (fn, index, '；'.join(semantic_notes)))
        if unused:
            chk.append('ℹ️ %s 还有 %d 次未列入期望参数组的调用' % (fn, len(unused)))

    if not chk:
        chk.append('实际调用：%s' % (json.dumps(actual, ensure_ascii=False) if actual else '无'))
    return '\n'.join(exp_lines) or '无', '\n'.join(chk)


def norm_label(label, eval_mode, failed):
    """failLabel 白名单校验：枚举外的值归一到该模式的兜底值，避免污染根因统计。"""
    allowed = LABELS_EXPECTED if eval_mode in ('expected', 'expected-semantic') else LABELS_JUDGE
    lb = (label or '').strip()
    if lb in allowed:
        return lb
    if not failed:
        return 'none'
    return 'result_mismatch' if eval_mode in ('expected', 'expected-semantic') else 'reply_deviation'


def _json_differences(expected, actual, path='$'):
    if type(expected) is not type(actual):
        return ['%s 类型不符（期望 %s，实际 %s）' % (
            path, type(expected).__name__, type(actual).__name__)]
    if isinstance(expected, dict):
        differences = []
        expected_keys, actual_keys = set(expected), set(actual)
        for key in sorted(expected_keys - actual_keys):
            differences.append('%s.%s 缺失' % (path, key))
        for key in sorted(actual_keys - expected_keys):
            differences.append('%s.%s 为多余字段' % (path, key))
        for key in sorted(expected_keys & actual_keys):
            differences.extend(_json_differences(expected[key], actual[key], '%s.%s' % (path, key)))
        return differences
    if isinstance(expected, list):
        differences = []
        if len(expected) != len(actual):
            differences.append('%s 数组长度不符（期望 %d，实际 %d）' % (
                path, len(expected), len(actual)))
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            differences.extend(_json_differences(
                expected_item, actual_item, '%s[%d]' % (path, index)))
        return differences
    return [] if expected == actual else ['%s 值不符（期望 %r，实际 %r）' % (path, expected, actual)]


def _strict_json_loads(text):
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('JSON 包含重复字段：%s' % key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError('JSON 包含非有限数字：%s' % value)

    return json.loads(text, object_pairs_hook=pairs_hook, parse_constant=reject_constant)


def _json_path_get(value, path):
    """读取简单 JSON Path（$.a.b 或 $.items.0），用于字段级诊断指标。"""
    if not isinstance(path, str) or not path.startswith('$.'):
        raise ValueError('metricPaths 仅支持 $.a.b 形式')
    current = value
    for part in path[2:].split('.'):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def deterministic_score(task, case, traj, names):
    case_id = case.get('id') or traj.get('caseId')
    if 'expectedOutput' not in case:
        return {
            'caseId': case_id, 'category': case.get('category') or traj.get('category'),
            'dimScores': {}, 'totalScore': None, 'verdict': 'INVALID',
            'failLabel': 'case_error', 'reason': '确定性评分用例缺少 expectedOutput',
            'differences': ['expectedOutput 缺失'], 'judgeTimes': 0, 'judgeCalled': False,
            'failedRuns': 0, 'parseError': False, 'scoreDisagreement': False,
            'evalMode': 'expected', 'scoreMode': 'expected',
            'comparisonMethod': 'deterministic', 'endReason': traj.get('endReason'),
        }
    expected = case.get('expectedOutput')
    actual_text = traj.get('output')
    if actual_text is None:
        actual_text = traj.get('finalOutput')
    actual_text = '' if actual_text is None else str(actual_text)
    rules = task.get('deterministicScoring') or {}
    if not isinstance(rules, dict):
        raise ValueError('deterministicScoring 必须为对象')
    output_format = str(rules.get('format') or task.get('expectedOutputFormat') or 'auto').lower()
    if output_format == 'auto':
        output_format = 'text' if isinstance(expected, str) else 'json'
    differences, format_error, component_scores = [], False, {}
    if output_format == 'json':
        raw = actual_text.strip()
        fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', raw, re.S | re.I)
        if fenced and rules.get('allowJsonFence') is True:
            raw = fenced.group(1)
        elif fenced:
            format_error = True
            differences.append('输出包含 Markdown 代码围栏，要求为纯 JSON')
        if not format_error:
            try:
                actual = _strict_json_loads(raw)
            except Exception as error:
                format_error = True
                differences.append('输出不是有效 JSON：%s' % error)
            else:
                differences.extend(_json_differences(expected, actual))
                metric_paths = rules.get('metricPaths') or {}
                if not isinstance(metric_paths, dict):
                    raise ValueError('deterministicScoring.metricPaths 必须为对象')
                for name, path in metric_paths.items():
                    try:
                        expected_component = _json_path_get(expected, path)
                        actual_component = _json_path_get(actual, path)
                        component_scores[name] = 100 if not _json_differences(
                            expected_component, actual_component, path) else 0
                    except (KeyError, ValueError, TypeError):
                        component_scores[name] = 0
    elif output_format == 'text':
        actual = actual_text.strip() if rules.get('trimText', True) else actual_text
        expected_text = str(expected).strip() if rules.get('trimText', True) else str(expected)
        if rules.get('caseSensitive', True) is False:
            actual, expected_text = actual.casefold(), expected_text.casefold()
        if actual != expected_text:
            differences.append('文本不一致（期望 %r，实际 %r）' % (expected_text, actual))
    else:
        raise ValueError('deterministicScoring.format 必须为 auto、text 或 json')
    passed = not differences
    total = 100 if passed else 0
    result_name = 'result' if 'result' in names else (names[0] if names else None)
    return {
        'caseId': case_id, 'category': case.get('category') or traj.get('category'),
        'dimScores': {result_name: total} if result_name else {},
        'totalScore': total, 'verdict': 'PASS' if passed else 'FAIL',
        'failLabel': 'none' if passed else ('format_error' if format_error else 'result_mismatch'),
        'reason': '程序确定性对照一致' if passed else '；'.join(differences[:3]),
        'differences': differences, 'judgeTimes': 0, 'judgeCalled': False,
        'failedRuns': 0, 'parseError': False, 'scoreDisagreement': False,
        'aggregate': 'deterministic', 'endReason': traj.get('endReason'),
        'evalMode': 'expected', 'scoreMode': 'expected',
        'comparisonMethod': 'deterministic',
        'componentScores': component_scores,
    }


def _pick_score_obj(data):
    """评分 JSON 可能被数组包裹：list 时取第一个含评分键的 dict（否则第一个 dict）。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and ('totalScore' in it or 'score' in it or 'verdict' in it or 'dimScores' in it):
                return it
        for it in data:
            if isinstance(it, dict):
                return it
    return None


def parse_score(content):
    """解析评分 JSON：整段优先（数组包裹自动剥开）；步骤化输出（推理步骤 + 最后 JSON）时，
    从各 '{'/'[' 起尝试解析，取最后一个含评分键的 dict（推理文本中的片段不误选）。"""
    txt = (content or '').strip()
    m = re.search(r'```(?:json)?\s*(.*?)\s*```', txt, re.S)
    if m:
        txt = m.group(1)
    try:
        obj = _pick_score_obj(json.loads(txt))
        if obj is not None:
            return obj
    except Exception:
        pass
    last = None
    i = 0
    while True:
        pos = -1
        for opener in ('{', '['):
            p = txt.find(opener, i)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos == -1:
            break
        i = pos + 1
        try:
            obj, _ = json.JSONDecoder().raw_decode(txt[pos:])
        except Exception:
            obj = None
        obj = _pick_score_obj(obj)
        if isinstance(obj, dict) and ('totalScore' in obj or 'score' in obj or 'verdict' in obj):
            last = obj
    if last is not None:
        return last
    m2 = re.search(r'\{.*\}', txt, re.S)
    if m2:
        obj = _pick_score_obj(json.loads(m2.group(0)))
        if obj is not None:
            return obj
    raise ValueError('评分输出无法解析为 JSON')


def recompute_total(dim_scores, names, weights):
    """按归一化权重重算总分（不信任模型自算，防超 100 / 算错）。"""
    vals, ws = [], []
    for n, w in zip(names, weights):
        v = (dim_scores or {}).get(n)
        if isinstance(v, (int, float)):
            vals.append(float(v))
            ws.append(w)
    if not vals:
        return None
    return round(sum(v * w for v, w in zip(vals, ws)) / (sum(ws) or 1), 1)


def judge_thresholds(task):
    """Judge 阈值必须来自任务确认，禁止使用通用60分或40分差。"""
    pass_threshold = task.get('judgePassThreshold')
    disagreement_threshold = task.get('scoreDisagreementThreshold')
    if (isinstance(pass_threshold, bool) or not isinstance(pass_threshold, (int, float))
            or not 0 <= pass_threshold <= 100):
        raise ValueError('Judge 模式必须显式配置 0-100 的 judgePassThreshold')
    if (isinstance(disagreement_threshold, bool)
            or not isinstance(disagreement_threshold, (int, float))
            or disagreement_threshold <= 0):
        raise ValueError('Judge 模式必须显式配置正数 scoreDisagreementThreshold')
    return float(pass_threshold), float(disagreement_threshold)


def score_once(config, task, case, traj, dims_text, names, weights, eval_mode, timeout, steps=False):
    """单次评分：judge 维度打分 或 expected 期望对照；steps=True 用步骤化模板
    （judge_score_step.md：先输出可见推理步骤再给 JSON；只切换模板，思考开关仍由配置决定）。"""
    inp, out = traj_io(traj)
    pass_threshold, _ = judge_thresholds(task)
    threshold_text = str(int(pass_threshold)) if pass_threshold.is_integer() else str(pass_threshold)
    policy_facts = task.get('judgePolicyFacts') or []
    if policy_facts:
        if isinstance(policy_facts, str):
            policy_text = policy_facts
        elif isinstance(policy_facts, list):
            policy_text = '\n'.join('- %s' % item for item in policy_facts)
        else:
            policy_text = json.dumps(policy_facts, ensure_ascii=False, indent=2)
        inp += ('\n\n任务政策事实（来自被测系统提示词，评分时视为已知且可信；'
                '不得把下列规定内的信息判为编造）：\n%s' % policy_text)
    expected = case.get('expectedOutput')
    if eval_mode == 'expected-semantic' and expected is not None:
        tpl = read_text(os.path.join(PROMPTS_DIR, 'expected_check.md'))
        out_fmt = task.get('outputFormat') or task.get('outputSpec') or '（未提供，按期望输出的结构推断）'
        if not isinstance(out_fmt, str):
            out_fmt = json.dumps(out_fmt, ensure_ascii=False, indent=1)
        filled = (tpl.replace('{taskDescription}', task.get('description') or '')
                     .replace('{expectedOutput}', json.dumps(expected, ensure_ascii=False, indent=1)
                              if not isinstance(expected, str) else expected)
                     .replace('{outputFormat}', out_fmt)
                     .replace('{passThreshold}', threshold_text)
                     .replace('{actualOutput}', out)
                     .replace('{input}', inp).replace('{output}', out))
        left = re.findall(r'\{[a-zA-Z][a-zA-Z0-9_]*\}', filled)
        if left:
            raise RuntimeError('对照评分提示词占位符未填充：%s' % '、'.join(sorted(set(left))))
        reply = llm.chat(config, 'judge', [{'role': 'user', 'content': filled}], temperature=0, timeout=timeout)
        data = parse_score(reply.get('content'))
        total = data.get('score')
        # 维度分：expected 模式本质是单一"对照正确性"，把对照分写入 result（或首个）维度，
        # 避免报告维度均分整体为空；其余维度不臆造分数。
        dim_scores = {}
        if isinstance(total, (int, float)) and names:
            dim_scores[names[0] if 'result' not in names else 'result'] = total
        failed = (total or 0) < pass_threshold
        return {'dimScores': dim_scores, 'totalScore': total,
                'verdict': 'FAIL' if failed else 'PASS',
                'reason': data.get('reason') or '', 'differences': data.get('differences') or [],
                'failLabel': norm_label(data.get('failLabel'), 'expected-semantic', failed),
                'evalMode': 'expected-semantic', 'scoreMode': 'expected-semantic',
                'comparisonMethod': 'judge'}

    exp_text, chk_text = tool_check(case, traj, task)
    tpl_name = 'judge_score_step.md' if steps else 'judge_score.md'
    tpl = read_text(os.path.join(PROMPTS_DIR, tpl_name))
    filled = (tpl.replace('{taskDescription}', task.get('description') or '')
                 .replace('{dimensionsText}', dims_text)
                 .replace('{input}', inp)
                 .replace('{output}', out)
                 .replace('{expectedToolsText}', exp_text)
                 .replace('{toolCheckText}', chk_text)
                 .replace('{passThreshold}', threshold_text))
    left = re.findall(r'\{[a-zA-Z][a-zA-Z0-9_]*\}', filled)
    if left:
        raise RuntimeError('评分提示词占位符未填充：%s' % '、'.join(sorted(set(left))))
    reply = llm.chat(config, 'judge', [{'role': 'user', 'content': filled}], temperature=0, timeout=timeout)
    data = parse_score(reply.get('content'))
    dim_scores = {k: v for k, v in (data.get('dimScores') or {}).items() if k in names}
    total = recompute_total(dim_scores, names, weights)
    if total is None:
        total = data.get('totalScore')
    failed = (total or 0) < pass_threshold
    return {'dimScores': dim_scores, 'totalScore': total, 'verdict': 'FAIL' if failed else 'PASS',
            'reason': data.get('reason') or '',
            'failLabel': norm_label(data.get('failLabel'), 'judge', failed),
            'toolCheck': chk_text, 'evalMode': 'judge', 'scoreMode': 'rubric'}


def aggregate(values):
    """多次评分聚合：2 次以内取平均；≥3 次取中位数（剔除异常值影响）。"""
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    if len(vals) <= 2:
        return round(sum(vals) / len(vals), 1)
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return round(med, 1)


def score_case(config, task, case, traj, dims_text, names, weights, eval_mode, times, timeout, steps=False):
    """多次评分聚合（≤2 次平均，≥3 次中位数）；分歧（分差 ≥40）重评一次；
    解析/调用失败的轮次**不计入聚合**（仅标记），仅当全部轮次都失败时才记 0 分。"""
    device_errors = traj.get('deviceErrors') or []
    execution_error = traj.get('error')
    if device_errors or execution_error or traj.get('endReason') in ('device_error', 'error'):
        reason = '；'.join(str(x.get('message') or x) if isinstance(x, dict) else str(x)
                          for x in device_errors) or str(execution_error or '测试装置错误')
        return {
            'caseId': case.get('id') or traj.get('caseId'),
            'category': case.get('category') or traj.get('category'),
            'dimScores': {}, 'totalScore': None, 'verdict': 'INVALID',
            'failLabel': 'device_error', 'reason': reason,
            'judgeTimes': 0, 'failedRuns': 0, 'parseError': False,
            'deviceError': True, 'endReason': traj.get('endReason'), 'scoreDisagreement': False,
            'evalMode': eval_mode, 'scoreMode': evaluation_mode.score_mode(eval_mode),
        }
    if eval_mode in ('expected', 'expected-semantic') and 'expectedOutput' not in case:
        return {
            'caseId': case.get('id') or traj.get('caseId'),
            'category': case.get('category') or traj.get('category'),
            'dimScores': {}, 'totalScore': None, 'verdict': 'INVALID',
            'failLabel': 'case_error', 'reason': '期望对照用例缺少 expectedOutput',
            'differences': ['expectedOutput 缺失'], 'judgeTimes': 0,
            'judgeCalled': False, 'failedRuns': 0, 'parseError': False,
            'deviceError': False, 'endReason': traj.get('endReason'),
            'scoreDisagreement': False, 'evalMode': eval_mode,
            'scoreMode': evaluation_mode.score_mode(eval_mode),
            'comparisonMethod': 'deterministic' if eval_mode == 'expected' else 'judge',
        }
    if eval_mode == 'expected':
        return deterministic_score(task, case, traj, names)
    pass_threshold, disagreement_threshold = judge_thresholds(task)
    runs, failed = [], []
    for _ in range(max(1, times)):
        for attempt in range(2):
            try:
                runs.append(score_once(config, task, case, traj, dims_text, names, weights, eval_mode, timeout, steps))
                break
            except Exception as e:
                if attempt == 1:
                    failed.append(str(e))

    totals = [r['totalScore'] for r in runs if isinstance(r.get('totalScore'), (int, float))]
    disagree = bool(totals) and (max(totals) - min(totals) >= disagreement_threshold)
    if disagree:
        try:
            extra = score_once(config, task, case, traj, dims_text, names, weights, eval_mode, timeout, steps)
            runs.append(extra)
            totals.append(extra['totalScore'])
        except Exception:
            pass

    if not runs:  # 全部轮次失败 → 记 0 分并标记
        return {
            'caseId': case.get('id') or traj.get('caseId'),
            'category': case.get('category') or traj.get('category'),
            'dimScores': {}, 'totalScore': 0, 'verdict': 'FAIL', 'failLabel': 'parse_error',
            'reason': '评分全部失败：%s' % '；'.join(failed[:2]),
            'judgeTimes': 0, 'failedRuns': len(failed), 'parseError': True,
            'endReason': traj.get('endReason'), 'scoreDisagreement': False,
            'evalMode': eval_mode, 'scoreMode': evaluation_mode.score_mode(eval_mode),
        }

    avg = aggregate(totals) or 0
    dim_avg = {}
    for n in names:
        vs = [(r.get('dimScores') or {}).get(n) for r in runs]
        agg = aggregate(vs)
        if agg is not None:
            dim_avg[n] = agg
    scored = [r for r in runs if isinstance(r.get('totalScore'), (int, float))]
    worst = min(scored, key=lambda r: r['totalScore']) if scored else runs[0]
    return {
        'caseId': case.get('id') or traj.get('caseId'),
        'category': case.get('category') or traj.get('category'),
        'dimScores': dim_avg, 'totalScore': avg,
        'verdict': 'FAIL' if avg < pass_threshold else 'PASS',
        'failLabel': worst.get('failLabel', 'none') if avg < pass_threshold else 'none',
        'reason': worst.get('reason', ''),
        'toolCheck': worst.get('toolCheck'),
        'judgeTimes': len(runs), 'scoreDisagreement': disagree,
        'aggregate': 'median' if len(totals) >= 3 else 'mean',
        'failedRuns': len(failed),
        'endReason': traj.get('endReason'),
        'parseError': False,
        'evalMode': eval_mode,
        'scoreMode': evaluation_mode.score_mode(eval_mode),
        'comparisonMethod': 'judge',
    }


def main():
    p = argparse.ArgumentParser(description='prompt-optimizer 评分器')
    p.add_argument('--config', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--trajectories', required=True, help='轨迹目录')
    p.add_argument('--cases', default=None, help='用例目录（取 expectedOutput / toolsExpect）')
    p.add_argument('--out', required=True, help='分数输出目录（scores/<round>）')
    p.add_argument('--regress', default=None, help='失败集写入路径（cases/regress.json）')
    p.add_argument('--concurrency', type=int, default=None)
    p.add_argument('--times', type=int, default=None,
                   help='仅 Judge 模式的每用例评分次数；确定性 expected 固定一次且不调用模型')
    p.add_argument('--prompts-dir', default=None, help='提示词目录（缺省用脚本同级的 ../prompts）')
    p.add_argument('--judge-steps', action='store_true',
                   help='用步骤化评分模板 judge_score_step.md（先输出可见推理步骤再给 JSON；'
                        '只切换模板，思考开关由 config 的 models.judge.reasoning 决定）')
    p.add_argument('--timeout', type=int, default=180)
    p.add_argument('--manifest', default=None, help='run_manifest.json 路径；缺省从输出目录推断运行根目录')
    p.add_argument('--run-id', default=None, help='要回填评分信息的运行清单 invocation ID')
    args = p.parse_args()

    global PROMPTS_DIR
    if args.prompts_dir:
        PROMPTS_DIR = args.prompts_dir
    if not os.path.isdir(PROMPTS_DIR):
        sys.exit('提示词目录不存在：%s（用 --prompts-dir 指定）' % PROMPTS_DIR)

    config = read_json(args.config)
    task = read_json(args.task)
    cases = load_cases_map(args.cases)
    dims_text, names, weights = dimensions_text(task)
    eval_mode = evaluation_mode.resolve_eval_mode(task)
    if eval_mode == 'tool-binary':
        sys.exit('tool-binary 必须使用 score_tool_binary.py')
    judge_used = evaluation_mode.uses_judge(eval_mode, task)
    if args.judge_steps and not judge_used:
        sys.exit('--judge-steps 仅适用于使用 Judge 的评分模式')
    if judge_used:
        try:
            judge_thresholds(task)
        except ValueError as error:
            sys.exit(str(error))
    times = (args.times or int(((config.get('budget') or {}).get('judgeTimesPerCase')) or 2)) if judge_used else 1
    # 评分并发独立于被测模型并发（评分模型可放开，被测是线上服务需限流）
    concurrency = args.concurrency or int(((config.get('budget') or {}).get('scoreConcurrency'))
                                          or config.get('concurrency') or 20)

    trajs = load_trajectories(args.trajectories)
    if not trajs:
        sys.exit('未找到轨迹：%s' % args.trajectories)

    os.makedirs(args.out, exist_ok=True)
    base_calls = len(trajs) * times if judge_used else 0
    judge_reasoning = bool(((config.get('models') or {}).get('judge') or {}).get('reasoning', False))
    mode_text = 'judge（步骤化）' if args.judge_steps else eval_mode
    if judge_used:
        log('【开始评分】%d 条轨迹｜方式 %s｜每用例 %d 次｜并发 %d｜judge 思考：%s｜维度权重已归一化'
            % (len(trajs), mode_text, times, concurrency, '开' if judge_reasoning else '关'))
    else:
        log('【开始评分】%d 条轨迹｜方式 expected（程序确定性对照）｜Judge 调用 0 次｜并发 %d'
            % (len(trajs), concurrency))
    if args.judge_steps:
        log('  步骤化模板只切换评分方式，思考开关由 config 的 models.judge.reasoning 决定：%s'
            % ('开思考 + 步骤化 = 最严模式（慢）' if judge_reasoning else '关思考 + 步骤化 = 快速模式'))
    if judge_used:
        log('  预估调用约 %d 次（%d 条 × %d 次）；若出现评分分歧会各补评 1 次，上限约 %d 次'
            % (base_calls, len(trajs), times, base_calls + len(trajs)))
        log('  聚合方式：单用例评分 ≤2 次取平均、≥3 次取中位数（剔除异常值影响）')
        log(dims_text)

    results = []
    idx = {'i': 0}

    def worker():
        while True:
            with _lock:
                if idx['i'] >= len(trajs):
                    return
                traj = trajs[idx['i']]
                idx['i'] += 1
            cid = traj.get('caseId')
            case = cases.get(cid) or {'id': cid}
            try:
                sc = score_case(config, task, case, traj, dims_text, names, weights, eval_mode, times, args.timeout,
                                args.judge_steps)
            except Exception as e:
                sc = {'caseId': cid, 'dimScores': {}, 'totalScore': 0, 'verdict': 'FAIL',
                      'failLabel': 'parse_error', 'reason': '评分异常：%s' % e}
            sc['taxonomyValues'] = case.get('taxonomyValues') or {}
            sc['caseRole'] = case.get('caseRole') or ''
            with open(os.path.join(args.out, '%s.json' % cid), 'w', encoding='utf-8') as f:
                json.dump(sc, f, ensure_ascii=False, indent=2)
            with _lock:
                results.append(sc)
                log('  [%d/%d] %s → %s %s%s' % (len(results), len(trajs), cid, sc['totalScore'],
                                                sc['verdict'],
                                                ' ⚠分歧' if sc.get('scoreDisagreement') else ''))

    threads = [threading.Thread(target=worker) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    fails = [r for r in results if r['verdict'] == 'FAIL']
    valid = [r for r in results if r.get('verdict') in ('PASS', 'FAIL')]
    passes = [r for r in valid if r.get('verdict') == 'PASS']
    totals = [r['totalScore'] for r in results if isinstance(r['totalScore'], (int, float))]
    labels = {}
    for r in fails:
        labels[r.get('failLabel', '?')] = labels.get(r.get('failLabel', '?'), 0) + 1
    dim_avg = {}
    for n in names:
        vs = [r['dimScores'].get(n) for r in results if isinstance((r.get('dimScores') or {}).get(n), (int, float))]
        if vs:
            dim_avg[n] = round(sum(vs) / len(vs), 1)

    summary = {
        'count': len(results),
        'scoredCount': len(valid),
        'invalidCount': len(results) - len(valid),
        'evalMode': eval_mode,
        'avgTotal': round(sum(totals) / len(totals), 1) if totals else 0,
        'passRate': round(100.0 * len(passes) / max(1, len(valid)), 1),
        'dimAvg': dim_avg, 'failLabels': labels,
        'disagreements': sum(1 for r in results if r.get('scoreDisagreement')),
        'parseErrors': sum(1 for r in results if r.get('parseError')),
        'failedJudgeRuns': sum(r.get('failedRuns') or 0 for r in results),
        'judgeCalls': sum(r.get('judgeTimes') or 0 for r in results),
        'comparisonMethod': 'judge' if judge_used else 'deterministic',
    }
    component_names = sorted({name for row in results for name in (row.get('componentScores') or {})})
    if component_names:
        summary['componentAccuracy'] = {
            name: round(sum((row.get('componentScores') or {}).get(name, 0) for row in results)
                        / max(1, len(results)), 1)
            for name in component_names
        }
    if eval_mode == 'expected':
        summary['dimNote'] = 'expected 为程序确定性 0/100 对照，Judge 调用 0 次；维度分仅写入 %s，非多维评估' \
                             % (('result' if 'result' in names else (names[0] if names else '-')))
    with open(os.path.join(args.out, '_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    run_dir = run_manifest.find_run_root(args.out)
    manifest_path = os.path.abspath(args.manifest) if args.manifest else os.path.join(run_dir, 'run_manifest.json')
    version_path = os.path.join(_BASE, 'VERSION')
    scorer_version = read_text(version_path).strip() if os.path.isfile(version_path) else 'unknown'
    run_manifest.record_scoring(
        manifest_path, args.trajectories, args.out, 'scorer.py',
        evaluation_mode.score_mode(eval_mode), scorer_version,
        run_id=args.run_id, comparison_method=summary['comparisonMethod'],
        judge_calls=summary['judgeCalls'],
    )

    # regress.json：写入全部失败用例，具体问题集再按根因显式选择。
    if args.regress:
        keep = [cases.get(r['caseId']) or {'id': r['caseId']} for r in
                sorted(fails, key=lambda r: r['totalScore'])]
        os.makedirs(os.path.dirname(os.path.abspath(args.regress)) or '.', exist_ok=True)
        with open(args.regress, 'w', encoding='utf-8') as f:
            json.dump(keep, f, ensure_ascii=False, indent=2)
        log('【失败集】已写入 %s（%d 条，优化前先跑这些）' % (args.regress, len(keep)))

    log('【评分完成】均分 %s｜通过率 %s%%｜失败 %d 条｜维度均分 %s｜根因分布 %s'
        % (summary['avgTotal'], summary['passRate'], len(fails),
           json.dumps(dim_avg, ensure_ascii=False), json.dumps(labels, ensure_ascii=False)))
    if summary.get('dimNote'):
        log('  注：%s' % summary['dimNote'])
    if summary['disagreements']:
        log('  评分分歧 %d 条（已补评并按中位数聚合）' % summary['disagreements'])
    if summary['failedJudgeRuns']:
        log('  评分调用失败 %d 次（该轮次不计入聚合，不拉低分数）' % summary['failedJudgeRuns'])
    if summary['parseErrors']:
        log('  全部轮次失败 %d 条（记 0 分并标记 parse_error）' % summary['parseErrors'])
    if summary['invalidCount']:
        log('  测试装置无效 %d 条（不计均分、通过率和失败回归集）' % summary['invalidCount'])


if __name__ == '__main__':
    main()
