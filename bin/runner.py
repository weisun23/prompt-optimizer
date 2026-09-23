#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt-optimizer 执行器：跑用例产出轨迹（仅标准库）。

职责（确定性逻辑，不做判断性工作）：
  变量渲染（候选池选值 + 整通快照）/ 多轮循环 / simulateMode 四分支 /
  工具触发（native tool_calls、text-protocol 函数名匹配）+ 提槽 / 显式用例级 Mock /
  终止协议状态机（completed / truncated / stalled / device_error）/ 并发调度 / 轨迹落盘

用法：
  python runner.py --config config.json --task tasks/<t>/task.json \
      --prompt runs/<r>/prompt_v1.md --cases cases/generated --out runs/<r>/trajectories \
      [--tools runs/<r>/tools_v1.json] [--concurrency 2] [--selftest 3] [--estimate]
"""
import argparse
import copy
import hashlib
import json
import os
import random
import re
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402
import case_correction_guard  # noqa: E402
import evaluation_mode  # noqa: E402
import model_profiles  # noqa: E402
import profile_constraints  # noqa: E402
import run_manifest  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 提示词目录：优先脚本同级的 ../prompts（工作区副本），可用 --prompts-dir 覆盖
PROMPTS_DIR = os.path.join(_BASE, 'prompts')
# 疑问判定：跨语言启发式（问号 + 中文疑问尾词 + 英文疑问/请求式开头）
QUESTION_MARKS = ('？', '?')
QUESTION_TAIL_ZH = ('吗', '呢', '吧', '对吗', '可以吗', '行吗', '怎么样', '如何', '多少', '哪个', '几位')
# 中文索取式（无问号也是在等回答）
QUESTION_HINT_ZH = ('请问', '麻烦您提供', '麻烦提供', '麻烦告知', '告诉我一下', '辛苦您告诉',
                    '请提供', '请告知', '需要您提供', '需要您确认', '您贵姓', '怎么称呼')
QUESTION_LEAD_EN = ('could you', 'can you', 'would you', 'will you', 'do you', 'did you', 'are you',
                    'is there', 'what', 'which', 'when', 'where', 'who', 'how', 'why',
                    'please provide', 'please confirm', 'please tell', 'may i')
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_cases(path):
    """目录（每文件一条或一个数组）或单个 JSON 文件。"""
    cases = []
    files = []
    if os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith('.json')]
    elif os.path.isfile(path):
        files = [path]
    for fp in files:
        data = read_json(fp)
        cases.extend(data if isinstance(data, list) else [data])
    return [c for c in cases if c.get('id')]


def pick_value(val):
    """候选池（数组）→ 选一个；单值原样。"""
    if isinstance(val, list) and val:
        return random.choice(val)
    return val


def snapshot_variables(case, task):
    """变量快照：候选池选值固化，整通对话共用。"""
    merged = {}
    merged.update(task.get('variables') or {})
    merged.update((case.get('chat') or {}).get('variables') or case.get('variables') or {})
    return {k: pick_value(v) for k, v in merged.items()}


def render(text, variables):
    """渲染 {var}：只替换已声明变量，未声明的占位符（指令伪占位符）原样保留。"""
    if not text:
        return text
    for k, v in (variables or {}).items():
        text = text.replace('{%s}' % k, str(v))
    return text


def mock_return(fn_name, arguments, tool_mock, tools, policy='required'):
    """显式 Mock 优先；仅用户明确选择 echo 时才生成参数回显成功结果。"""
    if isinstance(tool_mock, dict) and fn_name in tool_mock:
        preset = tool_mock[fn_name]
        if isinstance(preset, list):
            if not preset:
                raise RuntimeError('工具 %s 的顺序 toolMock 已耗尽' % fn_name)
            preset = preset.pop(0)
        return preset if isinstance(preset, str) else json.dumps(preset, ensure_ascii=False)
    if policy == 'echo':
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {'raw': args}
        args = args or {}
        echo = '、'.join('%s=%s' % (k, v) for k, v in args.items())
        desc = ''
        for t in (tools or []):
            fn = t.get('function') if isinstance(t, dict) and 'function' in t else t
            if (fn or {}).get('name') == fn_name:
                desc = (fn or {}).get('description') or ''
                break
        return json.dumps({
            'success': True,
            'msg': '（echo mock）%s：%s%s' % (fn_name, echo, ('｜' + desc) if desc else ''),
            'echo': args,
        }, ensure_ascii=False)
    raise RuntimeError('工具 %s 缺少显式 toolMock（toolMockPolicy=required）' % fn_name)


def tools_for_call(tools):
    """统一成 OpenAI tools 调用格式。"""
    out = []
    for t in (tools or []):
        if isinstance(t, dict) and 'function' in t:
            out.append(t)
        elif isinstance(t, dict) and t.get('name'):
            out.append({'type': 'function', 'function': t})
    return out


def tool_names(tools):
    names = []
    for t in (tools or []):
        fn = t.get('function') if isinstance(t, dict) and 'function' in t else t
        n = (fn or {}).get('name')
        if n:
            names.append(n)
    return names


def _expected_tool_names(case):
    names = case.get('toolsExpect') or []
    if isinstance(names, str):
        names = [names]
    return [name for name in names if isinstance(name, str) and name]


def _tool_result_success(result):
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return True
    return not isinstance(data, dict) or data.get('success') is not False


def _selftest_features(case, task):
    expected = _expected_tool_names(case)
    mocks = case.get('toolMock') or {}
    features = set()
    if not expected:
        features.add('no-tool')
    if len(expected) > 1 or len(set(expected)) < len(expected):
        features.add('multi-call')
    if isinstance(mocks, dict) and any(isinstance(value, list) for value in mocks.values()):
        features.add('sequence-mock')
    if isinstance(mocks, dict) and any(
            isinstance(value, dict) and value.get('success') is False
            for value in mocks.values()):
        features.add('failure-mock')
    if set(expected) & set(task.get('terminalTools') or []):
        features.add('terminal-tool')
    return features or {'ordinary'}


def select_selftest_cases(cases, task, limit):
    """按风险覆盖优先选择自检用例，而不是固定取前 N 条。"""
    remaining = list(cases)
    selected, covered = [], set()
    while remaining and len(selected) < max(0, limit):
        best_index = max(
            range(len(remaining)),
            key=lambda index: len(_selftest_features(remaining[index], task) - covered),
        )
        case = remaining.pop(best_index)
        selected.append(case)
        covered.update(_selftest_features(case, task))
    return selected


def detect_text_protocol_call(text, names, protocol_regex):
    """text-protocol 函数名识别：配了正则按正则；否则已知函数名集合独立词匹配。"""
    if not text:
        return None
    if protocol_regex:
        m = re.search(protocol_regex, text)
        if m:
            cand = m.group(1) if m.groups() else m.group(0)
            return cand if cand in names else None
    for n in names:
        if re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(n), text):
            return n
    return None


def is_question(text):
    """判断对方是否在等待回答（问句/索取信息）。跨语言启发式，语言中立。"""
    raw = (text or '').strip()
    if not raw:
        return False
    if any(m in raw[-40:] for m in QUESTION_MARKS):
        return True
    tail = raw.rstrip('~！!。.）)、， ')
    if tail.endswith(QUESTION_TAIL_ZH):
        return True
    if any(h in raw[-60:] for h in QUESTION_HINT_ZH):
        return True
    # 取最后一句做英文疑问/请求式判定
    last = re.split(r'[.!?\n。！？]', raw)
    last = next((s.strip().lower() for s in reversed(last) if s.strip()), '')
    return last.startswith(QUESTION_LEAD_EN)


def norm(text):
    return re.sub(r'[\s~！!。.，,？?、]', '', (text or ''))[:60]


def _sim_end_signal(text):
    """识别规范结束标记和纯括号舞台提示；括号外有真实话语时不误判。"""
    raw = (text or '').strip()
    if '[结束]' in raw:
        return True
    return bool(re.fullmatch(
        r'[（(][^）)]*(结束|完成|等待|不再多说|继续等|没别的话|不用了|挂断|通话已结束)[^）)]*[）)]', raw,
        re.I,
    ))


def simulate_user_turn(config, case, conversation, mode, script_line, timeout,
                       force_answer=False, variables=None):
    """调模拟用户模型得下一句用户话。"""
    tpl = read_text(os.path.join(PROMPTS_DIR, 'simulate_user.md'))
    flow = (case.get('chat') or {}).get('flowGuide') or case.get('flowGuide') or []
    facts = variables if variables is not None else (case.get('variables') or {})
    rendered_flow = [render(str(item), facts) for item in flow]
    filled = (tpl
              .replace('{scenario}', render(case.get('scenario') or '', facts))
              .replace('{userProfile}', render(case.get('userProfile') or '（无）', facts))
              .replace('{variables}', json.dumps(facts, ensure_ascii=False, indent=2) if facts else '（无）')
              .replace('{mode}', mode)
              .replace('{flowGuide}', '\n'.join('- %s' % f for f in rendered_flow)
                       if (rendered_flow and mode == 'guided') else '（无）')
              .replace('{scriptLine}', render(script_line or '（无）', facts)))
    convo = '\n'.join('%s: %s' % ({'user': '使用者', 'assistant': '被测系统', 'tool': '工具'}.get(m['role'], m['role']),
                                  m.get('content') or '')
                      for m in conversation if m.get('role') != 'system')
    request = '当前对话历史：\n%s\n\n请说出你（使用者）的下一句话：' % convo
    if force_answer:
        request += '\n对方正在等待回答。本轮必须用真实用户话语直接回答，不得结束，不得输出任何括号旁白。'
    msgs = [{'role': 'system', 'content': filled},
            {'role': 'user', 'content': request}]
    reply = llm.chat(config, 'simulator', msgs, timeout=timeout)
    return (reply.get('content') or '').strip()


def slot_fill(config, fn_name, tools, conversation, task_desc, timeout):
    """text-protocol：调提槽模型从对话抽参数。"""
    tpl = read_text(os.path.join(PROMPTS_DIR, 'slot_fill.md'))
    convo = '\n'.join('%s: %s' % (m['role'], m.get('content') or '')
                      for m in conversation if m.get('role') != 'system')
    filled = (tpl
              .replace('{functionName}', fn_name)
              .replace('{taskDescription}', task_desc or '')
              .replace('{toolsText}', json.dumps(tools_for_call(tools), ensure_ascii=False, indent=1))
              .replace('{conversation}', convo))
    reply = llm.chat(config, 'slotfiller', [{'role': 'user', 'content': filled}],
                     temperature=0, timeout=timeout)
    content = (reply.get('content') or '').strip()
    m = re.search(r'```(?:json)?\s*(.*?)\s*```', content, re.S)
    if m:
        content = m.group(1)
    try:
        data = json.loads(content)
        return data.get('arguments') or {}
    except Exception:
        return {}


def run_chat_case(config, case, task, prompt, tools, timeout):
    """对话型用例：多轮循环 + 工具 + 终止协议。返回轨迹 dict。"""
    chat_cfg = case.get('chat') or {}
    variables = snapshot_variables(case, task)
    mode = chat_cfg.get('simulateMode') or task.get('simulateMode') or (
        'guided' if chat_cfg.get('simulate', True) else 'off')
    scripted_exact = bool(chat_cfg.get('scriptedExact', task.get('scriptedExact', False)))
    max_turns = int(task.get('maxTurns') or 12)
    tool_mode = task.get('toolMode') or ('native' if tools else 'none')
    task_mock = task.get('toolMock') or {}
    case_mock = case.get('toolMock') or {}
    mock_setup_errors = []
    if not isinstance(task_mock, dict):
        mock_setup_errors.append({'type': 'invalid_tool_mock', 'message': 'task.toolMock 必须是对象'})
        task_mock = {}
    if not isinstance(case_mock, dict):
        mock_setup_errors.append({'type': 'invalid_tool_mock', 'message': 'case.toolMock 必须是对象'})
        case_mock = {}
    tool_mock = copy.deepcopy(task_mock)
    tool_mock.update(copy.deepcopy(case_mock))
    mock_policy = task.get('toolMockPolicy') or 'required'
    names = tool_names(tools)
    expected_names = set(_expected_tool_names(case))
    terminal_names = set(task.get('terminalTools') or [])
    scripted = [render(m.get('content'), variables) for m in chat_cfg.get('messages') or []
                if m.get('role') == 'user']

    system_prompt = render(prompt, variables)
    conversation = [{'role': 'system', 'content': system_prompt}]
    trace, tool_log, device_errors = [], [], list(mock_setup_errors)
    script_idx = 0
    end_reason, turns = 'completed', 0
    recent_user, recent_asst = [], []
    terminal_tool_called = False

    if not scripted:
        return {'caseId': case['id'], 'error': 'no user message', 'messages': []}
    first = scripted[0]
    script_idx = 1
    conversation.append({'role': 'user', 'content': first})
    trace.append({'role': 'user', 'content': first})

    while turns < max_turns:
        turns += 1
        # 被测模型回复
        try:
            use_tools = tools_for_call(tools) if tool_mode == 'native' else None
            reply = llm.chat(config, 'candidate', conversation, tools=use_tools, timeout=timeout)
        except Exception as e:
            end_reason = 'error: %s' % e
            break
        content = reply.get('content') or ''
        reasoning_content = reply.get('reasoning_content') or ''
        replay_reasoning = bool(reasoning_content and llm.reasoning_replay_enabled(config, 'candidate'))
        tcs = reply.get('tool_calls') or []
        asst = {'role': 'assistant', 'content': content, 'tool_calls': []}
        if replay_reasoning:
            asst['reasoning'] = reasoning_content

        # 工具触发
        pending = []
        native_calls = []  # native 模式的原始 tool_calls（含 id，标准回填用）
        if tool_mode == 'native' and tcs:
            for tc in tcs:
                native_calls.append(tc)
                fn = (tc.get('function') or tc)
                pending.append((fn.get('name'), fn.get('arguments')))
        elif tool_mode == 'text-protocol':
            hit = detect_text_protocol_call(content, names, task.get('toolProtocol'))
            if hit:
                context_reply = {'role': 'assistant', 'content': content}
                if replay_reasoning:
                    context_reply['reasoning_content'] = reasoning_content
                args = slot_fill(config, hit, tools, conversation + [context_reply],
                                 task.get('description'), timeout)
                pending.append((hit, args))

        flat_calls = []
        for i, (n, a) in enumerate(pending):
            item = {'name': n, 'arguments': a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}
            if i < len(native_calls) and native_calls[i].get('id'):
                item['id'] = native_calls[i]['id']
            flat_calls.append(item)
        asst['tool_calls'] = flat_calls
        trace.append(asst)
        recent_asst.append(norm(content))

        # 回填协议：native 用标准协议（assistant 保留 tool_calls + role:tool 带 tool_call_id），
        # 被测模型才能把工具结果和调用对应上——不规范回填（丢 tool_calls、结果当 user 文本）会导致
        # 原生函数调用模型重复调用/参数错乱；text-protocol 维持文本回填（模型不挂函数，按文本协议理解返回）
        if pending:
            if device_errors:
                end_reason = 'device_error'
                break
            known_names = set(names)
            # 模型输出未知/畸形函数名属于被测模型协议错误，不是测试装置缺 Mock。
            # 仅对“已知函数但缺少 Mock”判 device_error；未知函数返回确定性协议错误供评分。
            missing = [n for n, _ in pending
                       if n in known_names and n in expected_names and n not in tool_mock]
            if mock_policy == 'required' and missing:
                device_errors.append({'type': 'missing_tool_mock',
                                      'message': '缺少显式 toolMock：%s' % '、'.join(dict.fromkeys(missing))})
                end_reason = 'device_error'
                break
            try:
                results = []
                for n, a in pending:
                    if n not in known_names:
                        results.append(json.dumps({
                            'success': False,
                            'error': 'unknown_tool',
                            'message': '未知或畸形函数名：%s' % n,
                        }, ensure_ascii=False))
                    elif n not in expected_names:
                        results.append(json.dumps({
                            'success': False,
                            'error': 'unexpected_tool_call',
                            'message': '本用例未期望调用已知函数：%s' % n,
                        }, ensure_ascii=False))
                    else:
                        results.append(mock_return(n, a, tool_mock, tools, mock_policy))
            except Exception as e:
                device_errors.append({'type': 'invalid_tool_mock', 'message': str(e)})
                end_reason = 'device_error'
                break
            std_calls = []
            if tool_mode == 'native':
                for i, (n, a) in enumerate(pending):
                    call_id = (native_calls[i].get('id') if i < len(native_calls) and native_calls[i].get('id')
                               else 'call_t%d_%d' % (turns, i))
                    std_calls.append({'id': call_id, 'type': 'function', 'function': {
                        'name': n, 'arguments': a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}})
                assistant_message = {'role': 'assistant', 'content': content, 'tool_calls': std_calls}
                if replay_reasoning:
                    assistant_message['reasoning_content'] = reasoning_content
                conversation.append(assistant_message)
            else:
                assistant_message = {'role': 'assistant', 'content': content}
                if replay_reasoning:
                    assistant_message['reasoning_content'] = reasoning_content
                conversation.append(assistant_message)
            for i, (fn_name, args) in enumerate(pending):
                ret = results[i]
                if tool_mode == 'native':
                    call_id = std_calls[i]['id']
                    conversation.append({'role': 'tool', 'tool_call_id': call_id, 'content': ret})
                    trace.append({'role': 'tool', 'name': fn_name, 'tool_call_id': call_id, 'content': ret})
                else:
                    conversation.append({'role': 'user', 'content': '[工具 %s 返回] %s' % (fn_name, ret)})
                    trace.append({'role': 'tool', 'name': fn_name, 'content': ret})
                tool_log.append({'name': fn_name,
                                 'arguments': args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                                 'mock': ret, 'assistantTurn': turns})
                if fn_name in terminal_names and _tool_result_success(ret):
                    terminal_tool_called = True
            continue  # 回填后让被测模型继续
        assistant_message = {'role': 'assistant', 'content': content}
        if replay_reasoning:
            assistant_message['reasoning_content'] = reasoning_content
        conversation.append(assistant_message)

        # 用户下一句
        # 任务声明的终态工具成功后，让被测模型播报一次结果即结束。
        if terminal_tool_called:
            end_reason = 'completed'
            break
        # scripted 的参考台词已耗尽，且被测系统没有继续提问时，场景已经结束。
        # 继续调用模拟用户会凭空创造新诉求，把已完成流程拖到 truncated/stalled。
        if mode == 'scripted' and script_idx >= len(scripted) and not is_question(content):
            end_reason = 'completed'
            break
        if mode == 'off':
            if script_idx >= len(scripted):
                end_reason = 'completed'
                break
            nxt = scripted[script_idx]
            script_idx += 1
        elif mode == 'scripted' and scripted_exact and script_idx < len(scripted):
            # 严格参数契约用例直接使用已渲染脚本，避免模拟用户改写人数、日期或需求，
            # 造成实际输入与 expectedToolArgs 失配。脚本耗尽后的异常追问仍走下方自由兜底。
            nxt = scripted[script_idx]
            script_idx += 1
        else:
            script_line = scripted[script_idx] if (mode == 'scripted' and script_idx < len(scripted)) else ''
            try:
                nxt = simulate_user_turn(config, case, conversation, mode, script_line, timeout,
                                         variables=variables)
            except Exception as e:
                end_reason = 'error: %s' % e
                break
            if mode == 'scripted' and script_idx < len(scripted):
                script_idx += 1
            if not nxt:
                device_errors.append({'type': 'empty_simulator_output', 'message': '模拟用户返回空内容'})
                end_reason = 'device_error'
                break
            if _sim_end_signal(nxt):
                if is_question(content):
                    try:
                        nxt = simulate_user_turn(config, case, conversation, mode, script_line, timeout,
                                                 force_answer=True, variables=variables)
                    except Exception as e:
                        nxt = ''
                        device_errors.append({'type': 'simulator_retry_error', 'message': str(e)})
                    if not nxt or _sim_end_signal(nxt):
                        device_errors.append({'type': 'simulator_ended_while_question',
                                              'message': '对方等待回答时模拟用户仍尝试结束'})
                        end_reason = 'device_error'
                        break
                else:
                    end_reason = 'completed'
                    break

        conversation.append({'role': 'user', 'content': nxt})
        trace.append({'role': 'user', 'content': nxt})
        recent_user.append(norm(nxt))

        # 停滞检测：连续 3 轮双方都在复读
        if len(recent_user) >= 3 and len(set(recent_user[-3:])) == 1 \
                and len(recent_asst) >= 3 and len(set(recent_asst[-3:])) == 1:
            end_reason = 'stalled'
            break
    else:
        end_reason = 'truncated'

    return {
        'caseId': case['id'], 'category': case.get('category'), 'scenario': case.get('scenario'),
        'simulateMode': mode, 'variables': variables, 'messages': trace, 'toolCalls': tool_log,
        'endReason': end_reason, 'turns': turns, 'maxTurns': max_turns,
        'deviceErrors': device_errors,
        'finalOutput': next((m.get('content') for m in reversed(trace)
                             if m.get('role') == 'assistant'), ''),
    }


def run_inject_case(config, case, task, prompt, tools, timeout):
    """注入型用例：渲染 userTemplate 一次调用。"""
    variables = snapshot_variables(case, task)
    inj = case.get('inject') or {}
    tpl = inj.get('userTemplate') or task.get('userTemplate') or ''
    user_msg = tpl
    for k, v in (inj.get('inputs') or {}).items():
        user_msg = user_msg.replace('{%s}' % k, str(v))
    user_msg = render(user_msg, variables)
    system_prompt = render(prompt, variables)
    try:
        reply = llm.chat(config, 'candidate', [{'role': 'system', 'content': system_prompt},
                                               {'role': 'user', 'content': user_msg}],
                         tools=tools_for_call(tools) if (task.get('toolMode') == 'native' and tools) else None,
                         timeout=timeout)
    except Exception as e:
        return {'caseId': case['id'], 'error': str(e), 'messages': []}
    return {
        'caseId': case['id'], 'category': case.get('category'), 'scenario': case.get('scenario'),
        'variables': variables,
        'messages': [{'role': 'user', 'content': user_msg},
                     {'role': 'assistant', 'content': reply.get('content') or '',
                      'tool_calls': [{'name': (t.get('function') or t).get('name'),
                                      'arguments': (t.get('function') or t).get('arguments')}
                                     for t in reply.get('tool_calls') or []]}],
        'toolCalls': [], 'endReason': 'completed', 'turns': 1,
        'input': user_msg, 'output': reply.get('content') or '',
        'finalOutput': reply.get('content') or '',
    }


def estimate(cases, task, judge_times=2):
    """成本预估：用例数 × 平均轮次 × 每轮调用数。"""
    n = len(cases)
    chat_n = sum(1 for c in cases if (c.get('type') or 'chat') == 'chat')
    max_turns = int(task.get('maxTurns') or 12)
    avg_turns = max(3, min(max_turns, 6))
    per_turn = 2 if (task.get('simulateMode') or 'guided') != 'off' else 1
    if task.get('toolMode') == 'text-protocol':
        per_turn += 0.5
    exec_calls = int(chat_n * avg_turns * per_turn) + (n - chat_n)
    eval_mode = evaluation_mode.resolve_eval_mode(task)
    effective_judge_times = 1 if eval_mode == 'tool-binary' else judge_times
    score_calls = n * effective_judge_times if evaluation_mode.uses_judge(eval_mode, task) else 0
    total_calls = exec_calls + score_calls
    min_minutes, max_minutes = total_calls * 3 // 60, total_calls * 8 // 60
    log('【成本预估】用例 %d 条（对话型 %d）｜预计平均 %d 轮/通' % (n, chat_n, avg_turns))
    if score_calls:
        log('  执行阶段约 %d 次模型调用；评分阶段约 %d 次（每用例 %d 次）'
            % (exec_calls, score_calls, effective_judge_times))
    else:
        log('  执行阶段约 %d 次模型调用；评分阶段使用程序确定性对照，Judge 调用 0 次' % exec_calls)
    log('  合计约 %d 次调用；按每次 3~8 秒估算 ≈ %d~%d 分钟（并发可缩短）'
        % (total_calls, min_minutes, max_minutes))
    log('  如需分批，可用 --limit N 先跑一部分')
    return {'cases': n, 'chatCases': chat_n, 'avgTurns': avg_turns,
            'execCalls': exec_calls, 'judgeCalls': score_calls, 'totalCalls': total_calls,
            'minutesMin': min_minutes, 'minutesMax': max_minutes,
            'judgeTimes': effective_judge_times if score_calls else 0, 'evalMode': eval_mode}


def run_all(config, cases, task, prompt, tools, out_dir, concurrency, timeout):
    os.makedirs(out_dir, exist_ok=True)
    results, errors = [], []
    lock = threading.Lock()
    idx = {'i': 0}

    def worker():
        while True:
            with lock:
                if idx['i'] >= len(cases):
                    return
                case = cases[idx['i']]
                idx['i'] += 1
            ctype = case.get('type') or ('inject' if (case.get('inject') or task.get('userTemplate')) else 'chat')
            try:
                traj = (run_inject_case if ctype == 'inject' else run_chat_case)(
                    config, case, task, prompt, tools, timeout)
            except Exception as e:
                traj = {'caseId': case.get('id'), 'error': str(e), 'messages': [], 'endReason': 'error'}
            path = os.path.join(out_dir, '%s.json' % case['id'])
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(traj, f, ensure_ascii=False, indent=2)
            with lock:
                results.append(traj)
                if traj.get('error'):
                    errors.append(traj)
                log('  [%d/%d] %s → %s（%s 轮）' % (len(results), len(cases), case['id'],
                                                    traj.get('endReason'), traj.get('turns', '-')))

    threads = [threading.Thread(target=worker) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = {}
    for r in results:
        stats[r.get('endReason', '?')] = stats.get(r.get('endReason', '?'), 0) + 1
    log('【执行完成】%d 条 → %s' % (len(results), json.dumps(stats, ensure_ascii=False)))
    if errors:
        log('  失败 %d 条（已标记 error，不中断整批）' % len(errors))
    return results


def constraint_validation_errors(snapshot, receipt):
    errors = []
    if not isinstance(receipt, dict) or receipt.get('schemaVersion') != 1:
        errors.append('约束验证回执 schemaVersion 必须为 1')
        receipt = receipt if isinstance(receipt, dict) else {}
    if receipt.get('verdict') != 'PASS':
        errors.append('约束验证回执 verdict 必须为 PASS')
    if not receipt.get('validatedAt'):
        errors.append('约束验证回执缺少 validatedAt')
    if receipt.get('staticErrors') != []:
        errors.append('约束验证回执 staticErrors 必须为空数组')
    judge = receipt.get('judge')
    if not isinstance(judge, dict) or not judge.get('model'):
        errors.append('约束验证回执缺少 judge 模型信息')
    hard_ids = {item.get('id') for item in snapshot.get('hardConstraints') or [] if item.get('id')}
    results = receipt.get('results')
    if not isinstance(results, list):
        errors.append('约束验证回执 results 必须为数组')
        results = []
    result_ids = {item.get('id') for item in results if isinstance(item, dict) and item.get('id')}
    result_id_list = [item.get('id') for item in results
                      if isinstance(item, dict) and item.get('id')]
    if len(result_id_list) != len(set(result_id_list)):
        errors.append('约束验证回执 results 包含重复约束 ID')
    if result_ids != hard_ids:
        errors.append('约束验证回执 results 未完整覆盖硬约束')
    for item in results:
        if not isinstance(item, dict) or item.get('verdict') != 'PASS' or not str(item.get('evidence') or '').strip():
            errors.append('约束验证回执包含未通过或无证据条目')
            break
    if receipt.get('profileId') != snapshot.get('profileId'):
        errors.append('约束验证回执 profileId 与快照不一致')
    return errors


def rebaseline_gate_errors(marker, receipt, run_kind, current_case_set_hash,
                           selected_case_ids=None, limited=False):
    if marker is None:
        return []
    if not isinstance(marker, dict):
        return ['重建基线标记格式无效']
    if marker.get('schemaVersion') != 1:
        return ['重建基线标记 schemaVersion 必须为 1']
    if marker.get('status') not in ('pending', 'completed'):
        return ['重建基线标记 status 必须为 pending 或 completed']
    if marker.get('status') == 'completed':
        return []
    errors = []
    if not isinstance(receipt, dict) or receipt.get('schemaVersion') != 1:
        errors.append('待重建基线时必须提供有效的用例修正收据')
        receipt = {}
    if receipt.get('requiresRebaseline') is not True:
        errors.append('用例修正收据必须标记 requiresRebaseline=true')
    marker_cases = marker.get('appliedCaseIds')
    receipt_cases = receipt.get('appliedCaseIds')
    if (not isinstance(marker_cases, list) or not marker_cases or
            receipt_cases != marker_cases):
        errors.append('用例修正收据与重建基线标记的 appliedCaseIds 不一致')
    if run_kind != 'full':
        errors.append('用例修正后第一次运行必须使用 run-kind=full 重建基线')
    expected_hash = marker.get('caseSetSha256')
    if receipt.get('caseSetSha256') != expected_hash or current_case_set_hash != expected_hash:
        errors.append('用例修正收据、标记与当前用例集哈希不一致')
    expected_case_ids = marker.get('caseIds')
    if (not isinstance(expected_case_ids, list) or
            marker.get('caseCount') != len(expected_case_ids) or
            receipt.get('caseIds') != expected_case_ids or
            receipt.get('caseCount') != len(expected_case_ids)):
        errors.append('用例修正收据与标记缺少一致的完整用例清单')
    selected = list(selected_case_ids or [])
    if limited or sorted(selected) != sorted(expected_case_ids or []) or len(selected) != len(expected_case_ids or []):
        errors.append('重建基线必须执行完整用例集，禁止 --limit 或子集运行')
    return errors


def _find_rebaseline_marker(cases_path):
    current = os.path.abspath(cases_path if os.path.isdir(cases_path) else os.path.dirname(cases_path))
    while True:
        marker = os.path.join(current, '.rebaseline-required.json')
        if os.path.isfile(marker):
            return current, marker
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None, None


def main():
    p = argparse.ArgumentParser(description='prompt-optimizer 执行器')
    p.add_argument('--config', required=True, help='config.json')
    p.add_argument('--task', required=True, help='task.json')
    p.add_argument('--prompt', default=None,
                   help='当前提示词文件（prompt_vN.md，**必须是原始未渲染文本**）；'
                        '缺省回退 task.json 的 initialPrompt，两者都空则报错')
    p.add_argument('--cases', required=True, help='用例目录或 JSON 文件')
    p.add_argument('--out', required=True, help='轨迹输出目录')
    p.add_argument('--tools', default=None, help='工具定义 JSON（缺省用 task.json 里的 tools）')
    p.add_argument('--concurrency', type=int, default=None,
                   help='被测模型并发（同时跑几通对话；缺省取 config.concurrency 或 5）')
    p.add_argument('--limit', type=int, default=None, help='只跑前 N 条（分批用）')
    p.add_argument('--selftest', type=int, default=0, help='装置自检：只跑前 N 条并打印检查要点')
    p.add_argument('--run-id', default=None, help='本次运行在 run_manifest.json 中的唯一 ID；缺省取轨迹目录名')
    p.add_argument('--run-kind', default='unknown',
                   choices=['unknown', 'full', 'regression', 'stability', 'candidate', 'selftest'],
                   help='运行范围类型；自检会自动记为 selftest')
    p.add_argument('--manifest', default=None, help='run_manifest.json 路径；缺省写运行根目录')
    p.add_argument('--constraints', default=None,
                   help='用户确认的 optimization_constraints.json')
    p.add_argument('--constraint-validation', default=None,
                   help='check_constraints.py 生成的 constraint_validation.json')
    p.add_argument('--case-correction-receipt', default=None,
                   help='case_correction_guard.py 生成的用例修正收据；存在待重建标记时必填')
    p.add_argument('--estimate', action='store_true', help='只做成本预估，不执行')
    p.add_argument('--prompts-dir', default=None, help='提示词目录（缺省用脚本同级的 ../prompts）')
    p.add_argument('--timeout', type=int, default=180)
    args = p.parse_args()

    global PROMPTS_DIR
    if args.prompts_dir:
        PROMPTS_DIR = args.prompts_dir
    if not os.path.isdir(PROMPTS_DIR):
        sys.exit('提示词目录不存在：%s（用 --prompts-dir 指定）' % PROMPTS_DIR)

    config = read_json(args.config)
    task = read_json(args.task)
    prompt_task_type = model_profiles.infer_prompt_task_type(task, target_role='candidate')
    prompt = read_text(args.prompt) if args.prompt else (task.get('initialPrompt') or '')
    if not (prompt or '').strip():
        sys.exit('提示词为空：请用 --prompt 指定提示词文件，或在 task.json 填 initialPrompt。'
                 '绝不使用空提示词执行。')
    tools = read_json(args.tools) if args.tools else (task.get('tools') or [])
    cases = load_cases(args.cases)
    if not cases:
        sys.exit('未找到用例：%s' % args.cases)
    cand = (config.get('models') or {}).get('candidate') or {}
    profile_result = model_profiles.resolve_profile(config, 'candidate')
    recommended_profile = profile_result['profile']
    recommendation = model_profiles.build_recommendation(
        recommended_profile, role='candidate', task_type=prompt_task_type)

    # 基线保护：提示词必须是**原始未渲染**文本。若任务声明了变量、而提示词里已无对应 {占位符}，
    # 说明可能误传了渲染后的文件（变量被固化成上一次的值）→ 明确警告。
    declared = list((task.get('variables') or {}).keys())
    if declared:
        present = [v for v in declared if ('{%s}' % v) in prompt]
        if not present:
            log('⚠️ 警告：task.json 声明了变量 %s，但提示词中未发现任何对应 {占位符}。'
                '请确认 --prompt 指向的是**原始未渲染**的提示词文件（渲染后的文件会固化旧变量值）。'
                % '、'.join(declared[:5]))

    if args.estimate:
        estimate(cases, task, int(((config.get('budget') or {}).get('judgeTimesPerCase')) or 2))
        return

    constraint_snapshot = None
    constraint_validation = None
    if args.constraint_validation and not args.constraints:
        sys.exit('提供 --constraint-validation 时必须同时提供 --constraints')
    if args.constraints:
        constraint_snapshot = read_json(args.constraints)
        constraint_errors = profile_constraints.validate_constraint_snapshot(
            constraint_snapshot, model_profiles.load_registry())
        if constraint_errors:
            sys.exit('硬约束快照无效：%s' % '；'.join(constraint_errors))
        requires_validation = bool(constraint_snapshot.get('hardConstraints'))
        if requires_validation and constraint_snapshot.get('targetRole') != 'candidate':
            sys.exit('runner.py 只执行 candidate 提示词，硬约束 targetRole 必须为 candidate')
        if requires_validation and constraint_snapshot.get('promptTaskType') != prompt_task_type:
            sys.exit('硬约束快照与当前提示词任务类型不一致：%s != %s' % (
                constraint_snapshot.get('promptTaskType'), prompt_task_type))
        if requires_validation and not args.constraint_validation and not args.selftest:
            sys.exit('硬约束尚未验证：请先运行 check_constraints.py')
        if args.constraint_validation:
            constraint_validation = read_json(args.constraint_validation)
            if constraint_validation.get('verdict') != 'PASS':
                sys.exit('硬约束验证未通过，禁止执行正式回归')
            receipt_errors = constraint_validation_errors(constraint_snapshot, constraint_validation)
            if receipt_errors:
                sys.exit('硬约束验证回执无效：%s' % '；'.join(receipt_errors))
            prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
            if constraint_validation.get('promptSha256') != prompt_hash:
                sys.exit('硬约束验证中的候选提示词哈希不一致，必须重新验证')
            snapshot_payload = json.dumps(
                constraint_snapshot, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode('utf-8')
            snapshot_hash = hashlib.sha256(snapshot_payload).hexdigest()
            if constraint_validation.get('constraintsSha256') != snapshot_hash:
                sys.exit('硬约束验证中的约束快照哈希不一致，必须重新验证')

    case_root, rebaseline_marker_path = _find_rebaseline_marker(args.cases)
    rebaseline_marker = read_json(rebaseline_marker_path) if rebaseline_marker_path else None
    correction_receipt = (read_json(args.case_correction_receipt)
                          if args.case_correction_receipt else None)
    if rebaseline_marker_path:
        marker_pending = isinstance(rebaseline_marker, dict) and rebaseline_marker.get('status') == 'pending'
        current_case_snapshot = (case_correction_guard.case_set_snapshot(case_root)
                                 if marker_pending else None)
        effective_kind = 'selftest' if args.selftest else args.run_kind
        rebaseline_errors = rebaseline_gate_errors(
            rebaseline_marker, correction_receipt, effective_kind,
            current_case_snapshot['sha256'] if current_case_snapshot else None,
            [case.get('id') for case in cases], args.limit is not None)
        if rebaseline_errors:
            sys.exit('用例修正后重建基线门禁未通过：%s' % '；'.join(rebaseline_errors))
    if args.case_correction_receipt and not (
            isinstance(rebaseline_marker, dict) and rebaseline_marker.get('status') == 'pending'):
        sys.exit('提供了用例修正收据，但未找到待处理的重建基线标记')

    if args.selftest:
        cases = select_selftest_cases(cases, task, args.selftest)
        log('【装置自检】按风险覆盖选择 %d 条，请核验：多次调用、顺序/失败 Mock、终态工具、'
            '无工具用例、结束原因和模拟用户事实是否正确' % len(cases))
    elif args.limit:
        cases = cases[:args.limit]

    concurrency = args.concurrency or int(config.get('concurrency') or 5)
    log('【开始执行】%d 条用例，被测模型并发 %d，提示词 %s'
        % (len(cases), concurrency, os.path.basename(args.prompt)))
    run_all(config, cases, task, prompt, tools, args.out, concurrency, args.timeout)

    # 每个轨迹目录拥有自己的运行信息；运行根目录只保存可追加的 manifest。
    applied_profile = {'id': 'generic', 'label': '通用模型'}
    if constraint_snapshot and constraint_snapshot.get('status') in ('accepted', 'modified'):
        applied_profile = {
            'id': constraint_snapshot.get('profileId'),
            'label': constraint_snapshot.get('profileLabel') or constraint_snapshot.get('profileId'),
        }
    run_info = {
        'candidateModel': cand.get('model') or os.environ.get('LLM_MODEL') or '未配置',
        'candidateTemperature': cand.get('temperature'),
        'candidateReasoning': bool(cand.get('reasoning', False)),
        'candidateProvider': cand.get('provider'),
        'candidateModelFamily': cand.get('modelFamily'),
        'candidateApiStyle': cand.get('apiStyle') or 'openai-chat',
        'candidateProfile': applied_profile.get('id'),
        'candidateProfileLabel': applied_profile.get('label'),
        'candidateRecommendedProfile': recommended_profile.get('id'),
        'candidateRecommendedProfileLabel': recommended_profile.get('label'),
        'candidateProfileConfidence': profile_result.get('confidence'),
        'promptTaskType': prompt_task_type,
        'recommendedDirectiveIds': [item.get('id') for item in recommendation['hostOptimizationDirectives']],
        'runtimeRequirementIds': [item.get('id') for item in recommendation['runtimeRequirements']],
        'toolChoicePolicy': cand.get('toolChoicePolicy') or 'auto',
        'toolMode': task.get('toolMode') or ('native' if tools else 'none'),
        'simulateMode': task.get('simulateMode') or 'guided',
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, '_run_info.json'), 'w', encoding='utf-8') as f:
        json.dump(run_info, f, ensure_ascii=False, indent=2)

    run_dir = os.path.dirname(os.path.abspath(args.out))
    manifest_path = os.path.abspath(args.manifest) if args.manifest else os.path.join(run_dir, 'run_manifest.json')
    prompt_info = (run_manifest.file_snapshot(args.prompt, run_dir) if args.prompt
                   else dict(run_manifest.value_snapshot(prompt), source='task.initialPrompt'))
    tools_info = (run_manifest.file_snapshot(args.tools, run_dir) if args.tools
                  else dict(run_manifest.value_snapshot(tools), source='task.tools'))
    case_source = run_manifest.file_snapshot(args.cases, run_dir) if os.path.isfile(args.cases) else None
    case_info = {
        'source': case_source or {'path': run_manifest.relative_path(args.cases, run_dir)},
        'selectedSha256': run_manifest.value_snapshot(cases)['sha256'],
        'count': len(cases), 'caseIds': [case.get('id') for case in cases],
    }
    invocation_id = args.run_id or os.path.basename(os.path.normpath(args.out))
    manifest_record = {
        'id': invocation_id,
        'kind': 'selftest' if args.selftest else args.run_kind,
        'prompt': prompt_info, 'tools': tools_info, 'cases': case_info,
        'trajectoryDir': run_manifest.relative_path(args.out, run_dir),
        'model': {
            'model': run_info['candidateModel'],
            'temperature': run_info['candidateTemperature'],
            'reasoning': run_info['candidateReasoning'],
            'provider': run_info['candidateProvider'],
            'modelFamily': run_info['candidateModelFamily'],
            'apiStyle': run_info['candidateApiStyle'],
            'promptProfile': run_info['candidateProfile'],
            'profileLabel': run_info['candidateProfileLabel'],
            'recommendedProfile': run_info['candidateRecommendedProfile'],
            'recommendedProfileLabel': run_info['candidateRecommendedProfileLabel'],
            'promptProfileConfidence': run_info['candidateProfileConfidence'],
            'promptTaskType': run_info['promptTaskType'],
            'recommendedDirectiveIds': run_info['recommendedDirectiveIds'],
            'runtimeRequirementIds': run_info['runtimeRequirementIds'],
            'matchedBy': profile_result.get('matchedBy') or [],
            'toolChoicePolicy': run_info['toolChoicePolicy'],
            'capabilities': dict(cand.get('capabilities') or {}),
            'toolMode': run_info['toolMode'],
        },
    }
    if constraint_snapshot is not None:
        manifest_record['constraints'] = dict(
            run_manifest.file_snapshot(args.constraints, run_dir),
            profileId=constraint_snapshot.get('profileId'),
            status=constraint_snapshot.get('status'),
            count=len(constraint_snapshot.get('hardConstraints') or []),
            principleIds=list(constraint_snapshot.get('selectedPrincipleIds') or []),
            modelDirectiveIds=list(constraint_snapshot.get('selectedModelDirectiveIds') or []),
        )
    if constraint_validation is not None:
        manifest_record['constraintValidation'] = dict(
            run_manifest.file_snapshot(args.constraint_validation, run_dir),
            verdict=constraint_validation.get('verdict'),
        )
    if correction_receipt is not None:
        manifest_record['caseCorrection'] = dict(
            run_manifest.file_snapshot(args.case_correction_receipt, run_dir),
            appliedCaseIds=list(correction_receipt.get('appliedCaseIds') or []),
            caseSetSha256=correction_receipt.get('caseSetSha256'),
            caseCount=correction_receipt.get('caseCount'),
            caseIds=list(correction_receipt.get('caseIds') or []),
            requiresRebaseline=correction_receipt.get('requiresRebaseline'),
        )
        manifest_record['rebaseline'] = {
            'status': 'completed',
            'caseSetSha256': correction_receipt.get('caseSetSha256'),
        }
    run_manifest.record_execution(manifest_path, manifest_record)
    if isinstance(rebaseline_marker, dict) and rebaseline_marker.get('status') == 'pending':
        completed_marker = dict(rebaseline_marker)
        completed_marker.update({
            'status': 'completed',
            'completedAt': case_correction_guard.utc_now(),
            'baselineRunId': invocation_id,
            'manifestPath': run_manifest.relative_path(manifest_path, case_root),
        })
        case_correction_guard.write_json_atomic(rebaseline_marker_path, completed_marker)
    log('【运行清单】已追加 %s（%s）' % (manifest_path, invocation_id))


if __name__ == '__main__':
    main()
