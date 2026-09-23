# -*- coding: utf-8 -*-
"""从明确选择的评分轮次和轨迹生成自包含 HTML 报告。"""
import argparse
import glob
import html
import json
import os
import re
import sys

import score_protocol
import workflow_policy

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_TONE_WORDS = (
    "请问有什么可以帮", "很高兴为您服务", "这边帮您", "正在为您",
    "亲爱的用户", "感谢您的", "为您服务", "请您稍等", "请问您是",
)
FAIL_LABEL_CN = {
    "flow_break": "流程断裂", "info_error": "信息错误",
    "reply_deviation": "回复偏差", "tool_misuse": "工具误用",
    "parse_error": "评分失败", "device_error": "测试装置无效",
    "result_mismatch": "结果不符", "format_error": "格式错误",
    "missing_content": "内容缺失",
}
KIND_CN = {
    "full": "全量", "regression": "回归子集", "stability": "稳定性抽检",
    "candidate": "候选验证", "selftest": "装置自检", "unknown": "未标注",
}


def esc(value):
    return html.escape("" if value is None else str(value))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def _under_run(run_dir, path):
    if not path:
        return None
    result = path if os.path.isabs(path) else os.path.join(run_dir, path)
    result = os.path.abspath(result)
    root = os.path.abspath(run_dir)
    if os.path.commonpath([root, result]) != root:
        raise ValueError("报告路径越出运行目录：%s" % path)
    return result


def load_manifest(run_dir, manifest_path=None):
    path = manifest_path or os.path.join(run_dir, "run_manifest.json")
    if not os.path.isabs(path):
        path = os.path.join(run_dir, path)
    data = read_json(path)
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise ValueError("run_manifest.json 格式无效或版本不受支持")
    return data


def _manifest_entries(manifest):
    raw = (manifest or {}).get("rounds") or (manifest or {}).get("invocations") or []
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        round_id = item.get("id") or item.get("round")
        score_dir = item.get("scoreDir")
        if round_id and score_dir:
            entry = dict(item)
            entry["id"] = round_id
            entries.append(entry)
    return entries


def resolve_report_selection(run_dir, rounds=None, card_round=None, trajectories=None,
                             final_version=None, manifest_path=None):
    """解析报告输入。多轮旧运行必须显式选择，禁止扫描后猜测。"""
    manifest = load_manifest(run_dir, manifest_path)
    entries = _manifest_entries(manifest)
    by_id = {item["id"]: item for item in entries}
    if isinstance(rounds, str):
        requested = [x.strip() for x in rounds.split(",") if x.strip()]
    else:
        requested = list(rounds or [])
    if requested:
        selected = [dict(by_id.get(round_id) or {
            "id": round_id, "kind": "unknown",
            "scoreDir": os.path.join("scores", round_id),
        }) for round_id in requested]
    elif entries:
        marked = [item for item in entries if item.get("includeInReport")]
        if not marked and len(entries) > 1:
            raise ValueError("manifest 含多个评分轮但没有 includeInReport=true；请标记同口径轮次或使用 --rounds")
        selected = marked or entries
    else:
        scores_root = os.path.join(run_dir, "scores")
        names = sorted(
            name for name in os.listdir(scores_root)
            if os.path.isdir(os.path.join(scores_root, name))
        ) if os.path.isdir(scores_root) else []
        if len(names) > 1:
            raise ValueError("检测到多个分数目录但没有 run_manifest.json；请用 --rounds 显式选择")
        selected = [{"id": name, "kind": "unknown", "scoreDir": os.path.join("scores", name)}
                    for name in names]
    if not selected:
        raise ValueError("没有可用于报告的评分轮次")
    selected_ids = [item["id"] for item in selected]
    chosen_round = card_round or (manifest or {}).get("cardRound") or (manifest or {}).get("finalRound") or selected_ids[-1]
    if chosen_round not in selected_ids:
        raise ValueError("卡片评分轮次不在已选择轮次中：%s" % chosen_round)
    chosen_entry = next(item for item in selected if item["id"] == chosen_round)
    trajectory_dir = trajectories or chosen_entry.get("trajectoryDir")
    if not trajectory_dir:
        raise ValueError("未记录卡片轮次对应的轨迹目录；请提供 --trajectories")
    return {
        "manifest": manifest, "entries": selected, "roundIds": selected_ids,
        "cardRound": chosen_round, "trajectoryDir": trajectory_dir,
        "finalVersion": final_version or (manifest or {}).get("finalPrompt"),
        "decisionLog": (manifest or {}).get("decisionLog"),
    }


def find_scores(run_dir, selected_rounds=None):
    rounds = {}
    if isinstance(selected_rounds, dict):
        entries = selected_rounds.get("entries") or []
    elif selected_rounds:
        entries = [{"id": name, "scoreDir": os.path.join("scores", name)} for name in selected_rounds]
    else:
        entries = resolve_report_selection(run_dir)["entries"]
    for entry in entries:
        round_id = entry["id"]
        score_dir = _under_run(run_dir, entry.get("scoreDir") or os.path.join("scores", round_id))
        items, summary = score_protocol.load_score_round(score_dir)
        rounds[round_id] = items
        entry["summary"] = summary or {}
    return rounds


def find_trajectories(run_dir, directory=None):
    traj = {}
    tdir = _under_run(run_dir, directory or "trajectories")
    if not os.path.isdir(tdir):
        return traj
    for filename in sorted(os.listdir(tdir)):
        if not filename.endswith(".json") or filename.startswith("_"):
            continue
        data = read_json(os.path.join(tdir, filename))
        if isinstance(data, dict):
            case_id = data.get("caseId") or filename[:-5]
            traj[case_id] = data
    return traj


def validate_trajectory_coverage(scores, trajectories):
    score_ids, trajectory_ids = set(scores or {}), set(trajectories or {})
    if not trajectory_ids:
        raise ValueError("轨迹数量为 0，无法生成可信报告")
    if score_ids != trajectory_ids:
        missing, extra = sorted(score_ids - trajectory_ids), sorted(trajectory_ids - score_ids)
        raise ValueError("评分与轨迹用例集合不一致：缺轨迹 %s；多轨迹 %s" % (
            "、".join(missing[:8]) or "无", "、".join(extra[:8]) or "无"))


def find_prompts(run_dir):
    prompts, tools = [], []
    for path in glob.glob(os.path.join(run_dir, "prompt_v*.md")):
        match = re.search(r"prompt_v(\d+)\.md$", path)
        if match:
            with open(path, encoding="utf-8") as f:
                prompts.append((int(match.group(1)), os.path.basename(path), f.read()))
    for path in glob.glob(os.path.join(run_dir, "tools_v*.json")):
        match = re.search(r"tools_v(\d+)\.json$", path)
        if match:
            tools.append((int(match.group(1)), os.path.basename(path)))
    prompts.sort(key=lambda x: x[0])
    tools.sort(key=lambda x: x[0])
    return prompts, tools


def _decision_records(run_dir, manifest=None):
    paths = []
    if (manifest or {}).get("decisionLog"):
        paths.append(_under_run(run_dir, manifest["decisionLog"]))
    paths.extend([os.path.join(run_dir, "rounds.json"), os.path.join(run_dir, "tool_rounds.json")])
    for path in paths:
        data = read_json(path)
        if isinstance(data, list):
            return data
    return []


def choose_final_prompt(run_dir, prompts, manifest=None, explicit=None):
    requested = explicit or (manifest or {}).get("finalPrompt")
    by_name = {item[1]: item for item in prompts}
    if requested:
        if requested not in by_name:
            raise ValueError("最终提示词文件不存在：%s" % requested)
        return by_name[requested]
    records = _decision_records(run_dir, manifest)
    accepted = []
    for record in records:
        decision = record.get("decision") or record.get("suggestion")
        version = record.get("version")
        if version in by_name and decision in ("accepted", "production_best", "experimental_best", "baseline"):
            accepted.append(by_name[version])
    if accepted:
        return accepted[-1]
    if len(prompts) == 1:
        return prompts[0]
    return None


def tone_words(task_cfg):
    words = (task_cfg or {}).get("roleToneWords")
    return tuple(words) if words is not None else DEFAULT_TONE_WORDS


def label_cn(label):
    label = (label or "").strip()
    translated = FAIL_LABEL_CN.get(label)
    return "%s（%s）" % (translated, label) if translated else (label or "-")


def score_rows(rounds, entries=None):
    meta = {item["id"]: item for item in (entries or [])}
    order = [item["id"] for item in entries] if entries else sorted(rounds)
    rows = []
    for round_id in order:
        round_scores = rounds.get(round_id) or {}
        summary = score_protocol.summarize_scores(round_scores)
        entry = meta.get(round_id) or {}
        kind = entry.get("kind") or "unknown"
        model = entry.get("model") or {}
        scoring = entry.get("scoring") or {}
        sample = next(iter(round_scores.values()), {})
        rows.append({
            "round": round_id, "kind": kind, "kindName": KIND_CN.get(kind, kind),
            "model": model.get("model") or "未记录",
            "profile": model.get("promptProfile") or "generic",
            "avg": summary["avg"], "count": summary["scoredCount"],
            "total": summary["count"], "invalid": summary["invalidCount"],
            "pass": summary["correctCount"], "passRate": summary["passRate"],
            "scoreMode": entry.get("scoreMode") or sample.get("scoreMode") or "unknown",
            "comparisonMethod": scoring.get("comparisonMethod") or sample.get("comparisonMethod") or "unknown",
            "judgeCalls": scoring.get("judgeCalls", "未记录"),
        })
    return rows


def quality_section(trajs, scores, tone_wds):
    ends = {}
    for trajectory in trajs.values():
        reason = trajectory.get("endReason") or "?"
        ends[reason] = ends.get(reason, 0) + 1
    total, alerts, rows = len(trajs), [], []
    labels = {"completed": "正常完成", "truncated": "达轮次上限被截断",
              "stalled": "对话停滞提前截断", "error": "执行出错",
              "device_error": "测试装置无效"}
    for key, value in sorted(ends.items(), key=lambda x: -x[1]):
        pct = 100.0 * value / total
        rows.append("<tr><td>%s</td><td>%d</td><td>%.0f%%</td></tr>" % (esc(labels.get(key, key)), value, pct))
        if key == "truncated" and pct >= 20:
            alerts.append("达轮次上限的用例占 %.0f%%" % pct)
        if key in ("stalled", "error", "device_error") and value:
            alerts.append("%s %d 条" % (labels.get(key, key), value))
    invalid = sum(1 for score in scores.values() if score.get("verdict") == "INVALID")
    if invalid:
        alerts.append("测试装置无效 %d 条，未计入评分" % invalid)
    contaminated = []
    for case_id, trajectory in trajs.items():
        if trajectory.get("simulateMode") in ("off", "scripted"):
            continue
        for message in (trajectory.get("messages") or [])[1:]:
            text = message.get("content") or ""
            if message.get("role") == "user" and not text.startswith("[工具 ") and any(word in text for word in tone_wds):
                contaminated.append(case_id)
                break
    if contaminated:
        alerts.append("模拟用户疑似串角色 %d 条" % len(contaminated))
    table = "<table class='t'><tr><th>结束原因</th><th>条数</th><th>占比</th></tr>%s</table>" % "".join(rows)
    table += "".join("<p class='note'>%s</p>" % esc(item) for item in alerts) if alerts else "<p class='ok'>装置指标正常：轨迹完整，未发现执行异常</p>"
    return table


def rounds_section(run_dir, manifest=None):
    records = _decision_records(run_dir, manifest)
    records = [record for record in records
               if record.get("eventType") not in ("attempt-started", "milestone")]
    if not records:
        return ""
    rows = []
    names = {"accepted": "接受", "rolled_back": "回退", "baseline": "基线",
             "experimental_best": "实验最佳", "production_best": "生产最佳"}
    for record in records:
        decision = record.get("decision") or record.get("suggestion") or "-"
        cls = "pass" if decision in ("accepted", "production_best", "experimental_best", "baseline") else "fail" if decision == "rolled_back" else ""
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td><span class='v %s'>%s</span></td><td>%s</td></tr>" % (
            esc(record.get("round") or "-"), esc(record.get("version") or "-"),
            esc(record.get("scope") or record.get("count") or "-"), cls,
            esc(names.get(decision, decision)), esc(record.get("reason") or "")))
    return "<table class='t'><tr><th>轮次</th><th>版本</th><th>范围</th><th>决策</th><th>理由</th></tr>%s</table>" % "".join(rows)


def _card_payload(scores, trajs):
    payload, ordering = [], {"FAIL": 0, "INVALID": 1, "PASS": 2}
    for case_id, score in scores.items():
        trajectory, tool_names = trajs[case_id], []
        for message in trajectory.get("messages") or []:
            for call in message.get("tool_calls") or []:
                name = call.get("name") or (call.get("function") or {}).get("name")
                if name:
                    tool_names.append(name)
        payload.append({
            "caseId": case_id, "verdict": score.get("verdict") or "FAIL",
            "score": score.get("totalScore"), "reason": score.get("reason") or "",
            "failLabel": label_cn(score.get("failLabel")),
            "taxonomyValues": score.get("taxonomyValues") or {},
            "caseRole": score.get("caseRole") or "",
            "dimScores": score.get("dimScores") or {}, "messages": trajectory.get("messages") or [],
            "tools": tool_names,
        })
    return sorted(payload, key=lambda item: (ordering.get(item["verdict"], 9), item["caseId"]))


def _tools_for_final(tools, final_prompt, manifest):
    by_name = {item[1]: item for item in tools}
    requested = (manifest or {}).get("finalTools")
    if requested and requested in by_name:
        return by_name[requested]
    if final_prompt:
        match = re.search(r"prompt_v(\d+)\.md$", final_prompt[1])
        if match:
            return by_name.get("tools_v%s.json" % match.group(1))
    return tools[0] if len(tools) == 1 else None


def default_output_path(run_dir):
    run_dir = os.path.abspath(os.path.normpath(run_dir))
    task_dir = os.path.dirname(os.path.dirname(run_dir))
    workspace = os.path.dirname(os.path.dirname(task_dir))
    return os.path.join(workspace, "reports", os.path.basename(task_dir),
                        os.path.basename(run_dir), "report.html")


def report_gate_errors(workflow_status, user_requested=False):
    """拒绝为普通失败候选生成正式报告。"""
    if workflow_policy.should_generate_report(workflow_status, user_requested):
        return []
    return ["正式报告仅在 ACCEPTED 或用户明确要求时生成；失败候选应总结后继续优化"]


def profile_section(invocation):
    model = (invocation or {}).get("model") or {}
    constraints = (invocation or {}).get("constraints") or {}
    validation = (invocation or {}).get("constraintValidation") or {}
    profile_id = model.get("promptProfile")
    profile_label = model.get("profileLabel") or profile_id
    if not profile_label and not constraints and not validation:
        return "<p class='note'>本轮未记录模型画像与硬约束快照。</p>"
    status_names = {"accepted": "用户已接受", "modified": "用户修改后接受",
                    "rejected": "用户未采用"}
    verdict_names = {"PASS": "通过", "FAIL": "失败"}
    rows = [
        "模型画像：%s%s" % (
            esc(profile_label or "未记录"),
            "（%s）" % esc(profile_id) if profile_id and profile_id != profile_label else ""),
        "匹配置信度：%s" % esc(model.get("promptProfileConfidence") or "未记录"),
        "画像选择：%s；硬约束 %s 条" % (
            status_names.get(constraints.get("status"), "未记录"),
            esc(constraints.get("count", 0))),
        "硬约束验证：%s" % verdict_names.get(validation.get("verdict"), "未记录"),
    ]
    return "<div class='sum'>%s</div>" % "<br>".join(rows)


def build(run_dir, out_path, summary_text, selection=None):
    selection = selection or resolve_report_selection(run_dir)
    scores = find_scores(run_dir, selection)
    card_round = selection["cardRound"]
    card_scores = scores.get(card_round) or {}
    trajs = find_trajectories(run_dir, selection["trajectoryDir"])
    validate_trajectory_coverage(card_scores, trajs)
    prompts, tools = find_prompts(run_dir)
    manifest = selection.get("manifest") or {}
    final_prompt = choose_final_prompt(run_dir, prompts, manifest, selection.get("finalVersion"))
    final_tools = _tools_for_final(tools, final_prompt, manifest)
    rows = score_rows(scores, selection["entries"])
    entry = next(item for item in selection["entries"] if item["id"] == card_round)
    profile_html = profile_section(entry)

    task_path = os.path.join(os.path.dirname(os.path.dirname(os.path.normpath(run_dir))), "task.json")
    task_cfg = read_json(task_path) or {}
    quality_html = quality_section(trajs, card_scores, tone_words(task_cfg))
    decisions_html = rounds_section(run_dir, manifest)
    curve_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td></tr>" % (
            esc(row["round"]), esc(row["kindName"]), esc(row["model"]), esc(row["profile"]),
            esc(row["scoreMode"]), esc(row["comparisonMethod"]), esc(row["judgeCalls"]),
            row["avg"], row["pass"], row["count"], row["invalid"])
        for row in rows)
    curve_html = "<table class='t'><tr><th>轮次</th><th>范围</th><th>模型</th><th>画像</th><th>评分模式</th><th>对照方式</th><th>Judge调用</th><th>均分</th><th>通过</th><th>无效</th></tr>%s</table>" % curve_rows

    failures = [score for score in card_scores.values() if score.get("verdict") == "FAIL"]
    if failures:
        fail_rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(score.get("caseId")), esc(score.get("totalScore")), esc(label_cn(score.get("failLabel"))),
            esc((score.get("reason") or "")[:120])) for score in sorted(failures, key=lambda item: item.get("caseId") or ""))
        fail_html = "<table class='t'><tr><th>用例</th><th>分数</th><th>失败标签</th><th>理由</th></tr>%s</table>" % fail_rows
    else:
        fail_html = "<p class='ok'>当前卡片轮次无失败用例</p>"

    prompt_html = "<details class='prompt'><summary>最终提示词（%s）</summary><pre>%s</pre></details>" % (esc(final_prompt[1]), esc(final_prompt[2])) if final_prompt else "<p class='note'>尚未记录最终提示词；请在 manifest 中设置 finalPrompt。</p>"
    if final_tools:
        tool_data = read_json(os.path.join(run_dir, final_tools[1])) or {}
        tools_html = "<details class='prompt'><summary>最终工具定义（%s）</summary><pre>%s</pre></details>" % (esc(final_tools[1]), esc(json.dumps(tool_data, ensure_ascii=False, indent=2)))
    else:
        tools_html = ""

    run_info_path = _under_run(run_dir, os.path.join(selection["trajectoryDir"], "_run_info.json"))
    run_info = read_json(run_info_path) or read_json(os.path.join(run_dir, "_run_info.json")) or {}
    if run_info.get("candidateModel"):
        model_html = "被测模型：%s（温度 %s，思考：%s）｜<strong>本结论仅在该模型和本次清单范围内成立</strong>" % (
            esc(run_info["candidateModel"]), esc(run_info.get("candidateTemperature", "-")), "开" if run_info.get("candidateReasoning") else "关")
    else:
        model_html = "被测模型：未记录"
    summary_html = "<div class='sum'>%s</div>" % esc(summary_text).replace("\n", "<br>") if summary_text else "<div class='sum'>（未提供优化总结）</div>"
    payload_json = json.dumps(_card_payload(card_scores, trajs), ensure_ascii=False).replace("</", "<\\/")
    entry_scoring = entry.get("scoring") or {}
    scope_line = "评分轮次：%s｜范围：%s｜评分模式：%s｜对照方式：%s｜Judge调用：%s｜轨迹：%s｜有效用例：%d" % (
        esc(card_round), esc(KIND_CN.get(entry.get("kind") or "unknown", entry.get("kind") or "未标注")),
        esc(entry.get("scoreMode") or "未记录"), esc(entry_scoring.get("comparisonMethod") or "未记录"),
        esc(entry_scoring.get("judgeCalls", "未记录")), esc(selection["trajectoryDir"]), len(card_scores))

    document = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>提示词优化报告 · @@TASK@@</title><style>
:root{--bg:#f4f6f8;--surface:#fff;--line:#dfe3e8;--text:#20242a;--muted:#59636e;--accent:#155eef;--ok:#147d4f;--bad:#bd2c2c;--warn:#825500}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.6}
::selection{background:#cfe0ff;color:#102a56}:focus-visible{outline:3px solid #8bb4ff;outline-offset:2px}.wrap{max-width:1240px;margin:auto;padding:24px}.lede{color:var(--muted);overflow-wrap:anywhere}.scope{font-variant-numeric:tabular-nums;background:#eaf1ff;padding:10px 12px;border-radius:12px}h1{font-size:clamp(1.55rem,4vw,2.4rem);letter-spacing:-.03em;margin:.2em 0}h2{font-size:1.15rem;margin:2rem 0 .7rem}.twrap{overflow-x:auto;background:var(--surface);border-radius:14px}.t{width:100%;border-collapse:collapse;font-size:.88rem}.t th,.t td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.t th{white-space:nowrap}
.controls{display:grid;grid-template-columns:minmax(160px,2fr) repeat(4,minmax(120px,1fr));gap:10px;margin:12px 0}.controls label{font-size:.78rem;color:var(--muted)}.controls input,.controls select{width:100%;min-height:42px;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:8px 10px;font:inherit;font-size:1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:14px}.card{min-width:0;background:var(--surface);border-radius:14px;box-shadow:0 6px 24px rgba(31,45,61,.09);overflow:hidden}.card>summary{cursor:pointer;padding:12px 14px;font-weight:650;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.card[open]>summary{border-bottom:1px solid var(--line)}.badge{font-size:.74rem;padding:2px 8px;border-radius:999px}.PASS{background:#ddf6e9;color:#0b6840}.FAIL{background:#fde5e3;color:#9e211f}.INVALID{background:#fff0c7;color:#6b4700}.score{margin-inline-start:auto;font-variant-numeric:tabular-nums}.messages{padding:10px 12px;max-height:70vh;overflow:auto}.msg{max-width:94%;margin:7px 0;padding:8px 10px;border-radius:10px;overflow-wrap:anywhere}.msg.user{margin-inline-start:auto;background:#e5f1ff;color:#123f73}.msg.assistant{background:#e4f5ec;color:#164d35}.msg.tool{background:#fbefd4;color:#62480e}.role{font-size:.72rem;font-weight:700;opacity:.75}.tc{font-size:.78rem;background:#fff;border:1px dashed #aeb6c0;padding:4px 7px;margin-top:5px;border-radius:8px;overflow-wrap:anywhere}.foot{padding:10px 14px;color:var(--muted);font-size:.82rem;border-top:1px solid var(--line);overflow-wrap:anywhere}.pager{display:flex;gap:10px;align-items:center;justify-content:center;margin:16px 0}.pager button{min-height:40px;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:7px 14px}.pager button:disabled{opacity:.45}.note,.sum{background:#fff7dc;color:#5d4300;padding:10px 12px;border-radius:12px;overflow-wrap:anywhere}.ok{color:var(--ok)}details.prompt{background:var(--surface);border-radius:14px;padding:10px 12px;margin:8px 0}details.prompt pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:60vh;overflow:auto}@media(max-width:720px){body{font-size:16px}.wrap{padding:14px}.controls{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.t{font-size:.82rem}.messages{max-height:none}.score{margin-inline-start:0}}
</style></head><body><main class="wrap"><h1>提示词优化报告</h1><p class="lede">任务：@@TASK@@｜运行：@@RUN@@</p><p class="scope">@@SCOPE@@</p><p>@@MODEL@@</p><h2>模型画像与硬约束</h2>@@PROFILE@@<h2>评测质量</h2>@@QUALITY@@<h2>分数曲线</h2><div class="twrap">@@CURVE@@</div><h2>轮次决策</h2><div class="twrap">@@DECISIONS@@</div><h2>优化总结</h2>@@SUMMARY@@<h2>失败用例</h2><div class="twrap">@@FAILURES@@</div><h2>最终版本</h2>@@PROMPT@@@@TOOLS@@<h2>用例轨迹</h2><div class="controls"><label>用例 ID<input id="case-filter" type="search" placeholder="输入关键词"></label><label>结论<select id="verdict-filter"><option value="">全部</option><option>FAIL</option><option>INVALID</option><option>PASS</option></select></label><label>函数名<input id="tool-filter" type="search" placeholder="如 create_order"></label><label>动态分类<input id="taxonomy-filter" type="search" placeholder="维度或等级"></label><label>每页<select id="page-size"><option>20</option><option>50</option><option>100</option></select></label></div><p id="result-count" class="lede" aria-live="polite"></p><div id="cards" class="grid"></div><div class="pager"><button id="prev">上一页</button><span id="page-state"></span><button id="next">下一页</button></div></main><script id="report-data" type="application/json">@@DATA@@</script><script>
const all=JSON.parse(document.getElementById('report-data').textContent);let page=1;const byId=id=>document.getElementById(id);const filters=['case-filter','verdict-filter','tool-filter','taxonomy-filter','page-size'];function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node}function taxonomyText(x){return Object.entries(x.taxonomyValues||{}).map(([k,v])=>k+'='+((Array.isArray(v))?v.join(','):v)).join('｜')}function filtered(){const q=byId('case-filter').value.trim().toLowerCase(),v=byId('verdict-filter').value,t=byId('tool-filter').value.trim().toLowerCase(),y=byId('taxonomy-filter').value.trim().toLowerCase();return all.filter(x=>(!q||x.caseId.toLowerCase().includes(q))&&(!v||x.verdict===v)&&(!t||x.tools.some(n=>n.toLowerCase().includes(t)))&&(!y||(taxonomyText(x)+'｜'+(x.caseRole||'')).toLowerCase().includes(y)))}function messageNode(m){const box=el('div','msg '+(m.role||'user'));box.append(el('div','role',({user:'用户',assistant:'模型',tool:'工具'}[m.role]||m.role||'')));box.append(el('div','txt',m.content||''));for(const call of (m.tool_calls||[])){const fn=call.name||(call.function||{}).name||'',args=call.arguments??(call.function||{}).arguments??{};box.append(el('div','tc',fn+'('+((typeof args==='string')?args:JSON.stringify(args))+')'))}return box}function cardNode(x){const card=el('details','card');if(x.verdict==='FAIL')card.open=true;const head=el('summary');head.append(el('span','',x.caseId));head.append(el('span','badge '+x.verdict,x.verdict));head.append(el('span','score','总分 '+(x.score??'-')));card.append(head);const body=el('div','messages');const taxonomy=taxonomyText(x);if(taxonomy||x.caseRole)body.append(el('p','lede',(x.caseRole?x.caseRole+'｜':'')+taxonomy));for(const m of x.messages)body.append(messageNode(m));if(!x.messages.length)body.append(el('p','note','无消息记录'));card.append(body);card.append(el('div','foot',(x.failLabel&&x.failLabel!=='-'?x.failLabel+'｜':'')+(x.reason||'')));return card}function render(){const rows=filtered(),size=Number(byId('page-size').value),pages=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,pages);const host=byId('cards');host.replaceChildren();for(const row of rows.slice((page-1)*size,page*size))host.append(cardNode(row));byId('result-count').textContent='筛选结果 '+rows.length+' 条';byId('page-state').textContent=page+' / '+pages;byId('prev').disabled=page<=1;byId('next').disabled=page>=pages}for(const id of filters)byId(id).addEventListener('input',()=>{page=1;render()});byId('prev').onclick=()=>{page--;render()};byId('next').onclick=()=>{page++;render()};render();
</script></body></html>"""
    run_name = os.path.basename(os.path.normpath(run_dir))
    task_name = os.path.basename(os.path.dirname(os.path.dirname(os.path.normpath(run_dir))))
    replacements = {"@@TASK@@": esc(task_name), "@@RUN@@": esc(run_name), "@@SCOPE@@": scope_line,
                    "@@MODEL@@": model_html, "@@PROFILE@@": profile_html,
                    "@@QUALITY@@": quality_html, "@@CURVE@@": curve_html,
                    "@@DECISIONS@@": decisions_html or "<p>未记录轮次决策</p>", "@@SUMMARY@@": summary_html,
                    "@@FAILURES@@": fail_html, "@@PROMPT@@": prompt_html, "@@TOOLS@@": tools_html,
                    "@@DATA@@": payload_json}
    output = document
    for marker, value in replacements.items():
        output = output.replace(marker, value)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)
    print("已生成: %s" % out_path)


def main():
    parser = argparse.ArgumentParser(description="生成提示词优化 report.html")
    parser.add_argument("run_dir", help="runs/<run> 目录")
    parser.add_argument("--out", default=None)
    parser.add_argument("--summary", default=None)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--rounds", default=None, help="逗号分隔；旧运行多轮时必填")
    parser.add_argument("--card-round", default=None, help="用于失败清单和轨迹卡片的评分轮")
    parser.add_argument("--trajectories", default=None, help="卡片轮对应的轨迹目录")
    parser.add_argument("--final-version", default=None, help="明确的最终 prompt_vN.md")
    parser.add_argument("--workflow-status", default=None,
                        choices=["IN_PROGRESS", "CANDIDATE_REJECTED_CONTINUE", "ACCEPTED",
                                 "USER_STOPPED", "BUDGET_EXHAUSTED", "BLOCKED"])
    parser.add_argument("--user-requested", action="store_true", help="用户明确要求生成中间报告")
    args = parser.parse_args()
    gate_errors = report_gate_errors(args.workflow_status, args.user_requested)
    if gate_errors:
        sys.exit("报告门禁失败：%s" % "；".join(gate_errors))
    run_dir = os.path.abspath(args.run_dir)
    if not os.path.isdir(run_dir):
        sys.exit("运行目录不存在：%s" % run_dir)
    try:
        selection = resolve_report_selection(run_dir, args.rounds, args.card_round, args.trajectories,
                                             args.final_version, args.manifest)
    except ValueError as error:
        sys.exit("报告输入无效：%s" % error)
    out = args.out
    if not out:
        out = default_output_path(run_dir)
    summary = ""
    if args.summary and os.path.isfile(args.summary):
        with open(args.summary, encoding="utf-8") as f:
            summary = f.read()
    try:
        build(run_dir, out, summary, selection)
    except ValueError as error:
        sys.exit("报告生成失败：%s" % error)


if __name__ == "__main__":
    main()
