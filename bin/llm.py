#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt-optimizer 模型调用脚本（OpenAI 兼容，仅标准库，零 pip 依赖）。

用途：按角色调用配置的模型，供用例生成 / 模拟对话 / 评分等步骤使用。
多轮对话的编排由 Claude Code 完成（交替调用本脚本多次）。

用法示例：
  python llm.py --config config.json --role judge --messages msgs.json --output reply.json
  python llm.py --config config.json --role candidate --system prompt.txt --user "你好" --tools tools.json

模型配置来源（优先级）：--config 中 models.<role> > 环境变量 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def load_config(path):
    if path and os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def role_model_config(config, role):
    """取某角色的模型配置；只有 slotfiller 可显式复用 candidate。"""
    models = config.get('models') or {}
    role_cfg = dict(models.get(role) or {})
    reused_from = None
    if not role_cfg and role == 'slotfiller' and models.get('candidate'):
        role_cfg = dict(models['candidate'])
        reused_from = 'candidate'
    if not role_cfg and models:
        raise ValueError('缺少模型配置 models.%s；禁止静默复用其他角色模型' % role)
    base_url = role_cfg.get('base_url') or os.environ.get('LLM_BASE_URL')
    api_key = role_cfg.get('api_key') or os.environ.get(role_cfg.get('api_key_env') or 'LLM_API_KEY') \
        or os.environ.get('LLM_API_KEY')
    model = role_cfg.get('model') or os.environ.get('LLM_MODEL')
    temperature = role_cfg.get('temperature')
    reasoning = bool(role_cfg.get('reasoning', False))
    auth_type = role_cfg.get('auth_type') or ('bearer' if api_key else 'none')
    return {
        'base_url': base_url, 'api_key': api_key, 'model': model,
        'temperature': temperature, 'reasoning': reasoning,
        'auth_type': auth_type,
        'no_proxy': bool(role_cfg.get('no_proxy', False)),
        'thinking_control': role_cfg.get('thinking_control') or 'default',
        'provider': role_cfg.get('provider'),
        'model_family': role_cfg.get('modelFamily'),
        'api_style': role_cfg.get('apiStyle') or 'openai-chat',
        'prompt_profile': role_cfg.get('promptProfile') or 'generic',
        'tool_choice_policy': role_cfg.get('toolChoicePolicy') or 'auto',
        'capabilities': dict(role_cfg.get('capabilities') or {}),
        'reusedFrom': reused_from,
    }


def _tool_names(tools):
    names = []
    for tool in tools or []:
        function = tool.get('function') if isinstance(tool, dict) else None
        name = (function or tool or {}).get('name') if isinstance(function or tool, dict) else None
        if name:
            names.append(name)
    return names


def resolve_tool_choice(policy, tools):
    """把统一策略转换成 OpenAI Chat Completions 的 tool_choice。"""
    value = str(policy or 'auto').strip()
    if value in ('auto', 'none'):
        return value
    if value == 'required':
        if not tools:
            raise ValueError('toolChoicePolicy=required，但没有提供 tools')
        return value
    if value.startswith('named:'):
        name = value.split(':', 1)[1].strip()
        if not name or name not in _tool_names(tools):
            raise ValueError('指定函数不在 tools 中：%s' % (name or '（空）'))
        return {'type': 'function', 'function': {'name': name}}
    raise ValueError('不支持的 toolChoicePolicy：%s' % value)


def reasoning_replay_enabled(config, role='candidate'):
    """判断该角色的 assistant reasoning_content 是否必须回传。"""
    models = config.get('models') or {}
    role_cfg = dict(models.get(role) or {})
    if not role_cfg and role == 'slotfiller':
        role_cfg = dict(models.get('candidate') or {})
    capabilities = role_cfg.get('capabilities') or {}
    if 'reasoningReplay' in capabilities:
        return bool(capabilities['reasoningReplay'])
    provider = str(role_cfg.get('provider') or '').lower()
    model = str(role_cfg.get('model') or '').lower()
    return bool(role_cfg.get('reasoning', False) and
                (provider == 'deepseek' or 'deepseek' in model))


def read_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_text(path_or_inline):
    if path_or_inline and os.path.isfile(path_or_inline):
        with open(path_or_inline, 'r', encoding='utf-8') as f:
            return f.read()
    return path_or_inline


def call_model(url, headers, body, timeout, max_retries, no_proxy=False):
    """带重试的 POST 调用；返回响应 JSON。429/5xx 退避重试。
    400 且请求体含思考控制参数时：可能是厂商不支持该参数，去掉后重试一次（尽力关闭/开启思考，失败自动降级）。"""
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    last_err = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if no_proxy \
        else urllib.request.build_opener()
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:500]
            last_err = 'HTTP %s: %s' % (e.code, detail)
            if e.code == 400 and attempt < max_retries and ('thinking' in body or 'reasoning_effort' in body):
                body.pop('thinking', None)
                body.pop('reasoning_effort', None)
                data = json.dumps(body, ensure_ascii=False).encode('utf-8')
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_err)


def chat(config, role, messages, tools=None, temperature=None, reasoning=None,
         timeout=180, max_retries=2, tool_choice=None):
    """供 runner.py / scorer.py 复用的同进程调用：返回 {content, reasoning_content, tool_calls, finish_reason}。"""
    mc = role_model_config(config, role)
    if not mc['base_url'] or not mc['model'] or (mc['auth_type'] != 'none' and not mc['api_key']):
        raise RuntimeError('缺少模型配置（role=%s）：请在 config.json 的 models.%s 填写 base_url/model，'
                           '需要鉴权时还须填写 api_key'
                           % (role, role))
    body = {'model': mc['model'], 'messages': messages}
    temp = temperature if temperature is not None else mc['temperature']
    if temp is not None:
        body['temperature'] = temp
    use_reasoning = mc['reasoning'] if reasoning is None else reasoning
    if mc['thinking_control'] == 'qwen_vllm':
        body['chat_template_kwargs'] = {'enable_thinking': bool(use_reasoning)}
    elif use_reasoning:
        body['reasoning_effort'] = 'high'
    else:
        body['thinking'] = {'type': 'disabled'}
    if tools:
        body['tools'] = tools
        body['tool_choice'] = resolve_tool_choice(
            tool_choice if tool_choice is not None else mc['tool_choice_policy'], tools)
    elif tool_choice not in (None, 'auto', 'none') or mc['tool_choice_policy'] not in ('auto', 'none'):
        resolve_tool_choice(tool_choice if tool_choice is not None else mc['tool_choice_policy'], [])
    url = mc['base_url'].rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    if mc['auth_type'] == 'bearer':
        headers['Authorization'] = 'Bearer %s' % mc['api_key']
    result = call_model(url, headers, body, timeout, max_retries, no_proxy=mc['no_proxy'])
    choice = (result.get('choices') or [{}])[0]
    msg = choice.get('message', {}) or {}
    return {
        'content': msg.get('content'),
        'reasoning_content': msg.get('reasoning_content') or msg.get('reasoning'),
        'tool_calls': msg.get('tool_calls') or [],
        'finish_reason': choice.get('finish_reason'),
    }


def main():
    p = argparse.ArgumentParser(description='OpenAI 兼容模型调用（prompt-optimizer）')
    p.add_argument('--config', default=None, help='config.json 路径')
    p.add_argument('--role', default='candidate',
                   choices=['generate', 'simulator', 'candidate', 'slotfiller', 'judge'])
    p.add_argument('--messages', default=None, help='messages JSON 文件（多轮对话直接传完整数组）')
    p.add_argument('--system', default=None, help='system 文本或文件路径')
    p.add_argument('--user', default=None, help='user 文本或文件路径')
    p.add_argument('--tools', default=None, help='工具定义 JSON 文件（被测模型可用）')
    p.add_argument('--extra', default=None, help='额外 body 字段 JSON（如厂商思考参数 {"thinking":{"type":"enabled"}}）')
    p.add_argument('--output', default=None, help='结果输出文件（缺省打印到 stdout）')
    p.add_argument('--temperature', type=float, default=None, help='覆盖温度')
    p.add_argument('--reasoning', dest='reasoning', action='store_true', default=None, help='开启思考模式')
    p.add_argument('--no-reasoning', dest='reasoning', action='store_false', help='关闭思考模式')
    p.add_argument('--timeout', type=int, default=180)
    p.add_argument('--max-retries', type=int, default=2)
    p.add_argument('--tool-choice', default=None,
                   help='覆盖工具选择：auto、none、required 或 named:<函数名>')
    args = p.parse_args()

    cfg = load_config(args.config)
    mc = role_model_config(cfg, args.role)
    if not mc['base_url'] or not mc['model'] or (mc['auth_type'] != 'none' and not mc['api_key']):
        sys.stderr.write(
            '缺少模型配置（role=%s）：请提供 --config 中 models.%s，或环境变量 LLM_BASE_URL/LLM_MODEL；需要鉴权时再提供 API Key\n'
            % (args.role, args.role))
        sys.exit(2)

    # 组装 messages
    if args.messages:
        messages = read_json(args.messages)
    else:
        messages = []
        if args.system:
            messages.append({'role': 'system', 'content': read_text(args.system)})
        if args.user:
            messages.append({'role': 'user', 'content': read_text(args.user)})
        if not messages:
            sys.stderr.write('缺少输入：请用 --messages 或 --system/--user\n')
            sys.exit(2)

    body = {'model': mc['model'], 'messages': messages}
    temp = args.temperature if args.temperature is not None else mc['temperature']
    if temp is not None:
        body['temperature'] = temp
    # 思考开关按角色配置：Qwen/vLLM 使用 chat_template_kwargs.enable_thinking，其他模型沿用原控制参数。
    # 厂商不支持对应参数时服务端 400，call_model 会自动去掉参数重试（尽力而为，失败降级）。
    reasoning = args.reasoning if args.reasoning is not None else mc['reasoning']
    if mc['thinking_control'] == 'qwen_vllm':
        body['chat_template_kwargs'] = {'enable_thinking': bool(reasoning)}
    elif reasoning:
        body['reasoning_effort'] = 'high'
    else:
        body['thinking'] = {'type': 'disabled'}
    if args.tools:
        body['tools'] = read_json(args.tools)
        body['tool_choice'] = resolve_tool_choice(
            args.tool_choice if args.tool_choice is not None else mc['tool_choice_policy'],
            body['tools'])
    elif args.tool_choice not in (None, 'auto', 'none') or mc['tool_choice_policy'] not in ('auto', 'none'):
        resolve_tool_choice(args.tool_choice if args.tool_choice is not None
                            else mc['tool_choice_policy'], [])
    if args.extra:
        body.update(json.loads(args.extra))

    url = mc['base_url'].rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    if mc['auth_type'] == 'bearer':
        headers['Authorization'] = 'Bearer %s' % mc['api_key']

    try:
        result = call_model(url, headers, body, args.timeout, args.max_retries, no_proxy=mc['no_proxy'])
    except RuntimeError as e:
        sys.stderr.write('模型调用失败：%s\n' % e)
        sys.exit(1)

    # 解析回复：content + reasoning_content（思考，模型支持时）+ tool_calls
    choice = result.get('choices', [{}])[0]
    msg = choice.get('message', {}) or {}
    reply = {
        'content': msg.get('content'),
        'reasoning_content': msg.get('reasoning_content') or msg.get('reasoning'),
        'tool_calls': msg.get('tool_calls') or [],
        'finish_reason': choice.get('finish_reason'),
    }
    out = json.dumps(reply, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(out)
    else:
        print(out)


if __name__ == '__main__':
    main()
