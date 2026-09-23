#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-optimizer 用例生成器：按场景计划并发生成用例（仅标准库）。

职责（确定性逻辑）：
  按 plan 分类并发调用生成模型 / 填充 gen_cases.md 占位符（强制校验）/
  JSON 容错解析 / id 去重与冲突重命名 / 变量声明校验（区分可填变量与指令伪占位符）/ 落盘

判断性环节仍由 Claude 负责：场景计划确认、试点样例审查、全量审视、增删改。

用法：
  # 试点：每类先生成 N 条给用户审查
  python gen_cases.py --config config.json --task tasks/<t>/task.json \
      --plan runs/<r>/plan.json --coverage-plan runs/<r>/coverage-plan.json \
      --out cases/generated --pilot 1

  # 批量：审查通过后生成剩余（--skip-existing 跳过已生成的试点条数）
  python gen_cases.py ... --out cases/generated --full
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402
import evaluation_mode  # noqa: E402
import workflow_policy  # noqa: E402
import case_taxonomy  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(_BASE, 'prompts')
_lock = threading.Lock()  # 数据锁（results/idx）
_print_lock = threading.Lock()  # 日志锁（独立：Lock 不可重入，数据锁内调 log 会死锁）
TPL_PLACEHOLDERS = (
    '{taskDescription}', '{prompt}', '{userTemplate}', '{userInputTemplate}',
    '{category}', '{focus}', '{count}', '{taskType}', '{promptTaskType}', '{evalMode}', '{simulateMode}',
    '{userFormat}', '{varTable}', '{toolMode}', '{toolsText}', '{runtimePlaceholders}',
    '{terminalTools}', '{derivedToolArgs}', '{emptyEquivalentToolArgs}', '{deterministicScoring}',
    '{evaluationSourcePrecedence}',
    '{caseTaxonomy}',
)


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def strip_fence(text):
    t = (text or '').strip()
    m = re.search(r'```(?:json)?\s*(.*?)\s*```', t, re.S)
    if m:
        t = m.group(1)
    m2 = re.search(r'\[.*\]', t, re.S)
    if m2:
        t = m2.group(0)
    return t


def _function_schema(tool):
    """兼容 OpenAI tools 与扁平 function 描述，返回函数定义。"""
    if not isinstance(tool, dict):
        return {}
    fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
    return fn if isinstance(fn, dict) else {}


def tool_schema_map(task):
    """函数名 → 参数 schema；供生成提示词与生成后校验共用。"""
    out = {}
    for tool in task.get('tools') or []:
        fn = _function_schema(tool)
        name = fn.get('name')
        if name:
            params = fn.get('parameters') or fn.get('input_schema') or {}
            out[name] = params if isinstance(params, dict) else {}
    return out


def tools_text(task):
    """仅向生成模型暴露生成用例需要的工具契约，避免自然语言参数名。"""
    rows = []
    for tool in task.get('tools') or []:
        fn = _function_schema(tool)
        name = fn.get('name')
        if not name:
            continue
        params = fn.get('parameters') or fn.get('input_schema') or {}
        props = params.get('properties') if isinstance(params, dict) else {}
        required = set(params.get('required') or []) if isinstance(params, dict) else set()
        args = []
        for key, spec in (props or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            args.append({
                'name': key,
                'type': spec.get('type') or 'any',
                'required': key in required,
                'enum': spec.get('enum'),
                'description': spec.get('description') or '',
            })
        rows.append({'name': name, 'description': fn.get('description') or '', 'arguments': args})
    return json.dumps(rows, ensure_ascii=False, indent=2) if rows else '（无工具）'


def runtime_placeholders(task):
    """提取可渲染的 ASCII 运行时占位符，供生成模型建立用例变量快照。"""
    texts = [task.get('initialPrompt') or '', task.get('userTemplate') or '',
             task.get('userInputTemplate') or '']
    found = sorted(set(re.findall(r'\{([a-zA-Z][a-zA-Z0-9_]*)\}', '\n'.join(texts))))
    found = [name for name in found if name not in set(task.get('literalPlaceholders') or [])]
    return '、'.join('{%s}' % x for x in found) if found else '（无）'


def build_prompt(task, cat, count, cases_per_cat):
    """单次替换模板控制占位符；不扫描注入内容中的运行时占位符。"""
    tpl = read_text(os.path.join(PROMPTS_DIR, 'gen_cases.md'))
    var_table = task.get('variables') or {}
    values = {
        '{taskDescription}': task.get('description') or '',
        '{prompt}': task.get('initialPrompt') or '',
        '{userTemplate}': task.get('userTemplate') or '（无）',
        '{userInputTemplate}': task.get('userInputTemplate') or '（无）',
        '{category}': cat.get('category') or '',
        '{focus}': cat.get('focus') or '',
        '{count}': str(count),
        '{taskType}': task.get('taskType') or 'chat',
        '{promptTaskType}': task.get('promptTaskType') or 'generic',
        '{evalMode}': evaluation_mode.resolve_eval_mode(task),
        '{simulateMode}': task.get('simulateMode') or 'guided',
        '{userFormat}': task.get('userFormat') or 'placeholder',
        '{varTable}': json.dumps(var_table, ensure_ascii=False) if var_table else '（无预定义变量）',
        '{toolMode}': task.get('toolMode') or ('native' if task.get('tools') else 'none'),
        '{toolsText}': tools_text(task),
        '{runtimePlaceholders}': runtime_placeholders(task),
        '{terminalTools}': json.dumps(task.get('terminalTools') or [], ensure_ascii=False),
        '{derivedToolArgs}': json.dumps(task.get('derivedToolArgs') or [], ensure_ascii=False),
        '{emptyEquivalentToolArgs}': json.dumps(task.get('emptyEquivalentToolArgs') or [], ensure_ascii=False),
        '{deterministicScoring}': json.dumps(task.get('deterministicScoring') or {}, ensure_ascii=False),
        '{evaluationSourcePrecedence}': json.dumps(
            task.get('evaluationSourcePrecedence') or [], ensure_ascii=False),
        '{caseTaxonomy}': json.dumps(task.get('_caseTaxonomy') or {}, ensure_ascii=False, indent=2),
    }
    pattern = re.compile('|'.join(re.escape(p) for p in sorted(TPL_PLACEHOLDERS, key=len, reverse=True)))
    return pattern.sub(lambda m: values[m.group(0)], tpl)
def _gen_batch(config, task, cat, count, cases_per_cat, timeout):
    """生成一个批次；数量不符视为失败，便于缩批重试。"""
    prompt = build_prompt(task, cat, count, cases_per_cat)
    for attempt in range(2):
        try:
            reply = llm.chat(config, 'generate', [{'role': 'user', 'content': prompt}], timeout=timeout)
            data = json.loads(strip_fence(reply.get('content')))
            items = data if isinstance(data, list) else [data]
            items = [it for it in items if isinstance(it, dict)]
            if len(items) != count:
                raise ValueError('期望 %d 条，实际返回 %d 条' % (count, len(items)))
            for it in items:
                it.setdefault('category', cat.get('category'))
                it.setdefault('coverageItemIds', list(cat.get('coverageItemIds') or []))
                it.setdefault('taxonomyValues', dict(cat.get('taxonomyValues') or {}))
            return items
        except Exception as e:
            if attempt == 1:
                raise RuntimeError(str(e))


def _gen_batch_resilient(config, task, cat, count, cases_per_cat, timeout):
    """批次失败时自动二分缩批，直到单条；避免长 JSON 截断拖垮整个分类。"""
    try:
        return _gen_batch(config, task, cat, count, cases_per_cat, timeout)
    except Exception as e:
        if count <= 1:
            log('  ❌ 分类「%s」单条生成仍失败：%s' % (cat.get('category'), e))
            return []
        left = count // 2
        right = count - left
        log('  ⚠️ 分类「%s」批次 %d 条失败，自动缩为 %d + %d：%s'
            % (cat.get('category'), count, left, right, e))
        return (_gen_batch_resilient(config, task, cat, left, cases_per_cat, timeout)
                + _gen_batch_resilient(config, task, cat, right, cases_per_cat, timeout))


def gen_one_category(config, task, cat, count, cases_per_cat, timeout, batch_size=8):
    """按固定上限拆批生成单个分类，解析失败时自动缩批。"""
    items = []
    remaining = count
    while remaining > 0:
        size = min(max(1, batch_size), remaining)
        items.extend(_gen_batch_resilient(config, task, cat, size, cases_per_cat, timeout))
        remaining -= size
    return items


def validate_generation_plan(plan, coverage_plan, taxonomy):
    """场景计划必须引用并覆盖 coverage-plan 中所有待覆盖项。"""
    errors = workflow_policy.validate_coverage_plan(coverage_plan, taxonomy)
    if errors:
        return errors
    if not isinstance(plan, list) or not plan:
        return ["生成计划不能为空"]
    known = {item["id"] for item in coverage_plan["coverageItems"]}
    pending = set(workflow_policy.pending_coverage_ids(coverage_plan))
    referenced = set()
    for index, category in enumerate(plan, 1):
        ids = category.get("coverageItemIds") if isinstance(category, dict) else None
        if not isinstance(ids, list) or not ids or not all(
                isinstance(item, str) and item.strip() for item in ids):
            errors.append("生成计划第 %d 类 coverageItemIds 无效" % index)
            continue
        unknown = sorted(set(ids) - known)
        if unknown:
            errors.append("生成计划引用未知覆盖项：%s" % "、".join(unknown))
        referenced.update(ids)
        for error in case_taxonomy.validate_values(taxonomy, category.get('taxonomyValues')):
            errors.append("生成计划第 %d 类：%s" % (index, error))
    missing = sorted(pending - referenced)
    if missing:
        errors.append("生成计划未包含待覆盖项：%s" % "、".join(missing))
    return errors


# 用户口吻默认检测词（通用客服句式，高置信）：用户消息命中则提示审查，防生成用例角色错位。
# task.json 的 roleToneWords 可按领域覆盖，空数组 [] = 关闭检测（仅告警不阻断）。
DEFAULT_TONE_WORDS = ('请问有什么可以帮', '很高兴为您服务', '这边帮您', '正在为您', '亲爱的用户',
                      '感谢您的', '为您服务', '请您稍等', '请问您是')


def tone_words(task):
    """口吻检测关键词：roleToneWords 未配置 → 默认组；已配置（含空数组）→ 以配置为准。"""
    words = (task or {}).get('roleToneWords')
    return tuple(words) if words is not None else DEFAULT_TONE_WORDS


def _expected_args_map(raw):
    """把 expectedToolArgs 统一成 函数名→dict/list[dict]，保留同函数多组参数。"""
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


def _arg_groups(value):
    groups = value if isinstance(value, list) else [value]
    return [g for g in groups if isinstance(g, dict)]


def validate(cases, task, taxonomy=None):
    """校验并规整：变量必须可渲染，工具期望必须符合 schema，显式 Mock 不得缺失。"""
    declared = set((task.get('variables') or {}).keys())
    literal_placeholders = set(task.get('literalPlaceholders') or [])
    system_required = set(re.findall(r'\{([a-zA-Z][a-zA-Z0-9_]*)\}', task.get('initialPrompt') or '')) \
        - literal_placeholders
    schemas = tool_schema_map(task)
    tone_wds = tone_words(task)
    mock_policy = task.get('toolMockPolicy') or 'required'
    task_mock = task.get('toolMock') or {}
    task_mock_invalid = not isinstance(task_mock, dict)
    if task_mock_invalid:
        task_mock = {}
    source_precedence = task.get('evaluationSourcePrecedence') or []
    source_precedence_valid = (isinstance(source_precedence, list)
                               and all(isinstance(item, str) and item.strip()
                                       for item in source_precedence))
    seen, out, dropped, warns = set(), [], [], []
    for i, c in enumerate(cases, 1):
        if not isinstance(c, dict):
            dropped.append(('case-%03d' % i, '用例不是对象'))
            continue
        cid = c.get('id') or 'case-%03d' % i
        if cid in seen:
            n = 2
            while '%s-%d' % (cid, n) in seen:
                n += 1
            cid = '%s-%d' % (cid, n)
        c['id'] = cid
        seen.add(cid)

        if taxonomy is not None:
            taxonomy_errors = case_taxonomy.validate_values(taxonomy, c.get('taxonomyValues'))
            if taxonomy_errors:
                dropped.append((cid, 'taxonomyValues 无效：%s' % '；'.join(taxonomy_errors)))
                continue

        if not source_precedence_valid:
            dropped.append((cid, 'task.evaluationSourcePrecedence 必须为非空字符串数组'))
            continue
        if 'expectedOutput' in c and source_precedence:
            expected_source = c.get('expectedOutputSource')
            if expected_source not in source_precedence:
                dropped.append((cid, 'expectedOutputSource 必须来自 evaluationSourcePrecedence'))
                continue
            if not str(c.get('expectedOutputSourceEvidence') or '').strip():
                dropped.append((cid, '缺少 expectedOutputSourceEvidence，无法确认期望值权威来源'))
                continue

        chat = c.get('chat') or {}
        case_vars_raw = chat.get('variables') if chat.get('variables') is not None else c.get('variables')
        case_vars_raw = case_vars_raw or {}
        if not isinstance(case_vars_raw, dict):
            dropped.append((cid, 'variables 必须是对象'))
            continue
        case_vars = set(case_vars_raw)
        unresolved_system = system_required - declared - case_vars
        if unresolved_system:
            dropped.append((cid, 'system 占位符无取值：%s' % '、'.join(sorted(unresolved_system))))
            continue

        ctype = c.get('type') or ('inject' if c.get('inject') else 'chat')
        c['type'] = ctype
        if ctype == 'chat':
            msgs = chat.get('messages') or []
            user_texts = [m.get('content') or '' for m in msgs if isinstance(m, dict) and m.get('role') == 'user']
            if not user_texts:
                dropped.append((cid, '无 user 消息'))
                continue
            used_user = set(re.findall(r'\{([a-zA-Z][a-zA-Z0-9_]*)\}', '\n'.join(user_texts)))
            unresolved = used_user - declared - case_vars
            if unresolved:
                dropped.append((cid, 'user 占位符无取值：%s' % '、'.join(sorted(unresolved))))
                continue
            hits = sorted({kw for ut in user_texts for kw in tone_wds if kw in ut})
            if hits:
                warns.append((cid, '用户消息疑似客服口吻（命中「%s」），请审查是否角色错位' % '」「'.join(hits)))
        else:
            inj = c.get('inject') or {}
            tpl = inj.get('userTemplate') or task.get('userTemplate') or ''
            inputs = inj.get('inputs') or {}
            if not tpl or not isinstance(inputs, dict) or not inputs:
                dropped.append((cid, '注入型缺 userTemplate 或 inputs'))
                continue
            required_inputs = set(re.findall(r'\{([a-zA-Z][a-zA-Z0-9_]*)\}', tpl))
            unresolved = required_inputs - set(inputs) - declared - case_vars
            if unresolved:
                dropped.append((cid, '注入模板占位符无取值：%s' % '、'.join(sorted(unresolved))))
                continue

        visible_evidence = c.get('modelVisibleEvidence')
        if ('expectedOutput' in c or c.get('toolsExpect') or c.get('expectedToolArgs')) \
                and not isinstance(visible_evidence, list):
            dropped.append((cid, '缺少 modelVisibleEvidence；scenario 和 flowGuide 不能替代模型实际可见证据'))
            continue
        visible_sources = {
            'system': task.get('initialPrompt') or '',
            'user-message': '\n'.join(user_texts) if ctype == 'chat' else '%s\n%s' % (
                (c.get('inject') or {}).get('userTemplate') or task.get('userTemplate') or '',
                json.dumps((c.get('inject') or {}).get('inputs') or {}, ensure_ascii=False)),
            'variable': json.dumps(dict(task.get('variables') or {}, **case_vars_raw), ensure_ascii=False),
            'tool-result': json.dumps(c.get('toolMock') or task.get('toolMock') or {}, ensure_ascii=False),
        }
        evidence_errors = []
        for item in visible_evidence or []:
            if not isinstance(item, dict):
                evidence_errors.append('证据条目必须为对象')
                continue
            source = item.get('source')
            value = item.get('value')
            if source not in visible_sources:
                evidence_errors.append('source 无效：%s' % source)
            elif not isinstance(value, str) or not value.strip():
                evidence_errors.append('%s 缺少 value' % (item.get('fact') or '证据'))
            elif value not in visible_sources[source]:
                evidence_errors.append('%s 未出现在模型可见的 %s 中' % (
                    item.get('fact') or value, source))
        if evidence_errors:
            dropped.append((cid, '模型可见证据无效：%s' % '；'.join(evidence_errors)))
            continue

        expect_names = c.get('toolsExpect') or []
        if isinstance(expect_names, str):
            expect_names = [expect_names]
        expect_names = [n for n in expect_names if isinstance(n, str) and n]
        if expect_names:
            call_policy = c.get('expectedCallPolicy')
            if not isinstance(call_policy, dict):
                dropped.append((cid, '工具用例必须提供 expectedCallPolicy'))
                continue
            policy_mode = call_policy.get('mode')
            allowed_clarifications = call_policy.get('allowedClarifications')
            max_additional_turns = call_policy.get('maxAdditionalTurns')
            if policy_mode not in ('immediate', 'after-confirmation', 'eventual'):
                dropped.append((cid, 'expectedCallPolicy.mode 无效'))
                continue
            if (isinstance(allowed_clarifications, bool) or not isinstance(allowed_clarifications, int)
                    or allowed_clarifications < 0):
                dropped.append((cid, 'expectedCallPolicy.allowedClarifications 必须为非负整数'))
                continue
            if (isinstance(max_additional_turns, bool) or not isinstance(max_additional_turns, int)
                    or max_additional_turns < 0):
                dropped.append((cid, 'expectedCallPolicy.maxAdditionalTurns 必须为非负整数'))
                continue
            if policy_mode == 'immediate' and (allowed_clarifications or max_additional_turns):
                dropped.append((cid, 'immediate 策略不允许额外确认或额外轮次'))
                continue
        expect_args = _expected_args_map(c.get('expectedToolArgs') or {})
        c['expectedToolArgs'] = expect_args
        unknown_tools = (set(expect_names) | set(expect_args)) - set(schemas)
        if unknown_tools:
            dropped.append((cid, '工具名不在 schema：%s' % '、'.join(sorted(unknown_tools))))
            continue
        invalid_args = []
        for fn, value in expect_args.items():
            props = ((schemas.get(fn) or {}).get('properties') or {})
            if not props:
                continue
            for group in _arg_groups(value):
                invalid_args.extend('%s.%s' % (fn, key) for key in group if key not in props)
        if invalid_args:
            dropped.append((cid, 'expectedToolArgs 参数名不在 schema：%s' % '、'.join(sorted(set(invalid_args)))))
            continue

        call_counts = collections.Counter(expect_names)
        orphan_args = set(expect_args) - set(call_counts)
        if orphan_args:
            dropped.append((cid, 'expectedToolArgs 无对应 toolsExpect：%s' % '、'.join(sorted(orphan_args))))
            continue
        group_errors = []
        for fn, value in expect_args.items():
            actual_groups = len(_arg_groups(value))
            if actual_groups != call_counts[fn]:
                group_errors.append('%s 期望调用 %d 次但参数组 %d 组' % (
                    fn, call_counts[fn], actual_groups))
        if group_errors:
            dropped.append((cid, 'expectedToolArgs 参数组数量不一致：%s' % '；'.join(group_errors)))
            continue

        terminal_tools = set(task.get('terminalTools') or [])
        if any(name in terminal_tools for name in expect_names[:-1]):
            dropped.append((cid, '终态工具必须位于最后（toolsExpect）：%s' % '、'.join(expect_names)))
            continue

        case_mock = c.get('toolMock') or {}
        if not isinstance(case_mock, dict):
            dropped.append((cid, 'toolMock 必须是对象'))
            continue
        unknown_mocks = set(case_mock) - set(schemas)
        if unknown_mocks:
            dropped.append((cid, 'toolMock 工具名不在 schema：%s' % '、'.join(sorted(unknown_mocks))))
            continue
        expected_tools = set(expect_names) | set(expect_args)
        if task_mock_invalid and expected_tools:
            dropped.append((cid, 'task.toolMock 必须是对象'))
            continue
        missing_mocks = expected_tools - set(task_mock) - set(case_mock)
        if mock_policy == 'required' and missing_mocks:
            dropped.append((cid, '缺少显式 toolMock：%s' % '、'.join(sorted(missing_mocks))))
            continue

        short_mocks = []
        for fn, count in call_counts.items():
            preset = case_mock.get(fn) if fn in case_mock else task_mock.get(fn)
            if isinstance(preset, list) and len(preset) < count:
                short_mocks.append('%s 需要 %d 次但仅 %d 个' % (fn, count, len(preset)))
        if short_mocks:
            dropped.append((cid, '顺序 Mock 数量不足：%s' % '；'.join(short_mocks)))
            continue

        if ctype == 'chat' and (chat.get('simulateMode') == 'scripted' or task.get('simulateMode') == 'scripted') \
                and bool(chat.get('scriptedExact', task.get('scriptedExact', False))):
            fact_sources = user_texts + [task.get('initialPrompt') or '']
            fact_sources.append(json.dumps(task.get('variables') or {}, ensure_ascii=False))
            fact_sources.append(json.dumps(case_vars_raw, ensure_ascii=False))
            fact_text = '\n'.join(fact_sources)
            exempt = set(task.get('systemToolArgs') or []) | set(task.get('derivedToolArgs') or [])
            missing_facts = []
            for fn, value in expect_args.items():
                for group in _arg_groups(value):
                    for key, expected in group.items():
                        if key in exempt or ('%s.%s' % (fn, key)) in exempt:
                            continue
                        if isinstance(expected, (str, int, float)) and not isinstance(expected, bool):
                            text = str(expected).strip()
                            if text and text not in fact_text:
                                missing_facts.append('%s.%s=%s' % (fn, key, text))
            if missing_facts:
                warns.append((cid, 'scriptedExact 脚本未发现期望参数事实：%s；'
                               '如参数由系统推导，请加入 derivedToolArgs'
                               % '、'.join(missing_facts[:8])))
        out.append(c)
    return out, dropped, warns


def main():
    p = argparse.ArgumentParser(description='prompt-optimizer 用例生成器')
    p.add_argument('--config', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--plan', required=True,
                   help='覆盖驱动场景计划 JSON；每项必须显式给出 category、coverageItemIds、count 和 pilotCount')
    p.add_argument('--coverage-plan', required=True,
                   help='coverage-plan.json；场景计划必须覆盖其中全部 pending 项')
    p.add_argument('--taxonomy', required=True,
                   help='用户确认的 case-taxonomy.json；分类维度和等级不得由 Skill 写死')
    p.add_argument('--out', required=True, help='用例输出目录')
    p.add_argument('--pilot', type=int, default=None, help='可选：覆盖计划 pilotCount 的本次人工覆盖值')
    p.add_argument('--full', action='store_true', help='批量模式：每类生成 count 条（试点已生成的会跳过同 id）')
    p.add_argument('--concurrency', type=int, default=None, help='生成总并发（缺省取 budget.genConcurrency 或 20）')
    p.add_argument('--prompts-dir', default=None)
    p.add_argument('--timeout', type=int, default=300)
    args = p.parse_args()

    global PROMPTS_DIR
    if args.prompts_dir:
        PROMPTS_DIR = args.prompts_dir
    if not os.path.isdir(PROMPTS_DIR):
        sys.exit('提示词目录不存在：%s' % PROMPTS_DIR)

    config = read_json(args.config)
    task = read_json(args.task)
    plan_raw = read_json(args.plan)
    plan = plan_raw.get('plan') if isinstance(plan_raw, dict) else plan_raw
    if not plan:
        sys.exit('计划为空：%s' % args.plan)
    coverage_plan = read_json(args.coverage_plan)
    taxonomy = read_json(args.taxonomy)
    task['_caseTaxonomy'] = taxonomy
    taxonomy_hash = file_sha256(args.taxonomy)
    expected_taxonomy_hash = ((coverage_plan.get('taxonomy') or {}).get('sha256')
                              if isinstance(coverage_plan, dict) else None)
    if taxonomy_hash != expected_taxonomy_hash:
        sys.exit('覆盖计划中的 taxonomy.sha256 与当前分类体系不一致')
    plan_errors = validate_generation_plan(plan, coverage_plan, taxonomy)
    if plan_errors:
        sys.exit('覆盖计划门禁失败：%s' % '；'.join(plan_errors))

    budget = config.get('budget') or {}
    batch_size = max(1, int(budget.get('genBatchSize') or 8))
    concurrency = args.concurrency or int(budget.get('genConcurrency') or 20)

    # 数量由覆盖计划显式给出；已有输出按分类计数，避免试点后批量重复生成。
    existing_counts = collections.Counter()
    if os.path.isdir(args.out):
        for filename in os.listdir(args.out):
            if not filename.endswith('.json'):
                continue
            try:
                data = read_json(os.path.join(args.out, filename))
            except (OSError, ValueError):
                continue
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                if isinstance(row, dict) and row.get('category'):
                    existing_counts[row['category']] += 1
    targets = []
    for cat in plan:
        if not isinstance(cat, dict) or not isinstance(cat.get('count'), int) or cat['count'] <= 0:
            sys.exit('覆盖计划每项必须显式提供正整数 count')
        if not isinstance(cat.get('coverageItemIds'), list) or not cat['coverageItemIds']:
            sys.exit('覆盖计划每项必须提供 coverageItemIds')
        pilot_count = args.pilot if args.pilot is not None else cat.get('pilotCount')
        if not args.full and (not isinstance(pilot_count, int) or pilot_count <= 0):
            sys.exit('试点运行必须由覆盖计划提供正整数 pilotCount，或显式传 --pilot')
        remaining = max(0, cat['count'] - existing_counts[cat.get('category')])
        targets.append((cat, remaining if args.full else min(remaining, pilot_count)))
    planned = sum(n for _, n in targets)
    max_cases = budget.get('maxCasesPerRun')
    if max_cases is not None and planned > int(max_cases):
        sys.exit('本次计划 %d 条超过用户明确设置的资源预算 maxCasesPerRun=%s；'
                 '不得静默删减覆盖项，请提高预算或分批执行' % (planned, max_cases))

    mode = '批量' if args.full else '试点'
    log('【用例生成·%s】%d 个分类，本次共 %d 条，生成总并发 %d'
        % (mode, len(targets), planned, concurrency))

    results, idx = [], {'i': 0}

    def worker():
        while True:
            with _lock:
                if idx['i'] >= len(targets):
                    return
                cat, n = targets[idx['i']]
                idx['i'] += 1
            if n <= 0:
                continue
            items = gen_one_category(config, task, cat, n, cat['count'], args.timeout, batch_size)
            with _lock:
                results.extend(items)
                log('  ✓ %s → %d 条' % (cat.get('category'), len(items)))

    threads = [threading.Thread(target=worker) for _ in range(max(1, min(concurrency, len(targets))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cases, dropped, warns = validate(results, task, taxonomy)
    os.makedirs(args.out, exist_ok=True)
    existing = {f[:-5] for f in os.listdir(args.out) if f.endswith('.json')}
    written = 0
    for c in cases:
        cid = c['id']
        if cid in existing:  # 试点已写过的同 id 不覆盖
            n = 2
            while '%s-%d' % (cid, n) in existing:
                n += 1
            cid = '%s-%d' % (cid, n)
            c['id'] = cid
        with open(os.path.join(args.out, '%s.json' % cid), 'w', encoding='utf-8') as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        existing.add(cid)
        written += 1

    log('【生成完成】落盘 %d 条 → %s' % (written, args.out))
    if dropped:
        log('  丢弃 %d 条：%s' % (len(dropped), '；'.join('%s(%s)' % d for d in dropped[:5])))
    if warns:
        log('  提示 %d 条含未声明占位符（指令引用可忽略）：%s'
            % (len(warns), '；'.join('%s: %s' % w for w in warns[:3])))
    log('  ⏭ 下一步：把用例展示给用户审查（试点看格式 / 批量做全量审视），确认后再执行')


if __name__ == '__main__':
    main()
