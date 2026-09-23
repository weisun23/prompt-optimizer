# 评分与运行清单契约

## 评分对象

所有新评分文件至少包含：

```json
{
  "caseId": "case-001",
  "scoreMode": "rubric | expected | expected-semantic | tool-binary",
  "totalScore": 100,
  "verdict": "PASS | FAIL | INVALID",
  "failLabel": "none | tool_misuse | device_error | ...",
  "reason": "简要理由"
}
```

`expected` 表示程序确定性对照，并额外记录 `comparisonMethod=deterministic`、`judgeCalled=false`、`judgeTimes=0`。`expected-semantic` 才使用 Judge；两者不得混为同一评分口径。

使用 Judge 的任务必须在 `task.json` 显式填写 `judgePassThreshold` 和 `scoreDisagreementThreshold`，并记录其正式口径或用户确认来源。模板会把该任务阈值传给 Judge，评分器也按同一值复核；不得使用隐含的通用 60 分或固定分歧阈值。

固定 JSON、分类与提槽可在 `deterministicScoring.metricPaths` 中声明需要单独统计的 JSONPath。每条评分写入 `componentScores`，摘要聚合为 `componentAccuracy`，用于识别局部失效；总体正确率不能掩盖字段级退化。

`tool-binary` 每条只能为0或100，并额外保留 `score`、`errors` 和 `judgeCalled`。旧文件只有 `score` 时，`score_protocol.py` 会补出统一字段。

`INVALID` 的 `totalScore` 为 `null`，不计均分、通过率、修复或退化。

## 摘要

`_summary.json` 是轮次元数据，不是用例。统一字段：

- `count`：全部记录数
- `scoredCount`：PASS/FAIL 数量
- `invalidCount`：INVALID 数量
- `avg` / `score`：有效记录平均分
- `passRate`
- 二值模式额外包含 `correctCount`、`wrongCount`、`binaryValues`

## run_manifest.json

```json
{
  "schemaVersion": 1,
  "finalPrompt": "prompt_v4.md",
  "finalTools": "tools_v4.json",
  "cardRound": "v4-full-r2",
  "decisionLog": "tool_rounds.json",
  "invocations": [
    {
      "id": "v4-full-r2",
      "kind": "full",
      "prompt": {"path": "prompt_v4.md", "sha256": "..."},
      "tools": {"path": "tools_v4.json", "sha256": "..."},
      "cases": {"count": 111, "caseIds": ["..."], "selectedSha256": "..."},
      "trajectoryDir": "trajectories-v4-full-r2",
      "scoreDir": "scores/v4-full-r2",
      "scoreMode": "tool-binary",
      "scoring": {"comparisonMethod": "judge", "judgeCalls": 111},
      "model": {
        "provider": "qwen",
        "modelFamily": "qwen3",
        "model": "Qwen3.8-27B",
        "reasoning": false,
        "apiStyle": "openai-chat",
        "promptProfile": "qwen-nonthinking",
        "toolChoicePolicy": "required"
      },
      "constraints": {"path": "optimization_constraints.json", "sha256": "...", "status": "accepted", "count": 4},
      "constraintValidation": {"path": "constraint_validation.json", "sha256": "...", "verdict": "PASS"},
      "caseCorrection": {"path": "case-correction-receipt.json", "sha256": "...", "caseSetSha256": "...", "caseCount": 111, "caseIds": ["..."]},
      "rebaseline": {"status": "completed", "caseSetSha256": "..."},
      "scorer": {"name": "score_tool_binary.py", "version": "1.4.6"},
      "includeInReport": true
    }
  ]
}
```

`kind` 可取 `full`、`regression`、`stability`、`candidate`、`selftest`、`unknown`。只有统计口径一致的 invocation 才应同时设置 `includeInReport=true`。

## 比较规则

- 默认要求两轮有效用例 ID 集合完全一致。
- 回归子集使用 `compare_rounds.py --allow-subset`，只比较交集。
- 子集输出必须同时显示基线数量、当前数量和交集数量。
- 全量与子集的总体均分不能直接作为同口径提升。
- `compare_rounds.py` 只生成修复、退化、分类变化和分数变化证据，不使用通用修复率或噪声阈值自动接受/回退。最终结论由 `acceptance-policy.json` 和 `iteration_guard.py` 给出，或由用户显式记录。

## 实验账本与恢复状态

- `rounds.json` 使用追加事件保存 `attempt-started`、`attempt-completed`、`milestone` 和 `comparison-recorded`；不得通过重写同名轮次删除旧结论。
- 每个实际改动的提示词候选必须拥有独立 `prompt_vN.md`，并记录父版本；重复运行同一文件时只新增运行 invocation。
- `optimization-state.json` 是当前状态快照，可更新；`rounds.json` 是历史账本，只追加。
- `OPTIMIZATION_SUMMARY.md` 由账本和状态生成，用于人类阅读和任务恢复；它不能覆盖 manifest、评分文件或哈希中的冲突事实。

## 报告规则

- 优先读取 manifest 中的 `includeInReport`、`cardRound`、`finalPrompt` 和 `finalTools`。
- 没有 manifest 且存在多个分数目录时，使用 `--rounds`、`--card-round`、`--trajectories` 和 `--final-version` 明确选择。
- 评分存在但轨迹为0，或卡片轮评分/轨迹 ID 集合不同，报告生成失败。
- 不通过最大文件编号推断最终版本。
- 使用模型画像时必须记录用户选择和约束验证快照；验证提示词或约束哈希不一致时不得复用。
- 不同 candidate、画像或主观 Judge 的分数分开呈现，避免误作同口径提升。
- 用例修正收据 `requiresRebaseline=true` 时必须创建新基线 invocation，并记录新的用例集哈希；修正前后不能视为同一统计口径。
- 普通失败候选、局部门禁失败和全量回退不生成正式报告，只保存 manifest、评分、迭代摘要与下一步。`report_gen.py` 仅接受 `--workflow-status ACCEPTED`，或在用户明确要求中间报告时使用 `--user-requested`。
- 评分文件保留用例的 `taxonomyValues` 和 `caseRole`；HTML 报告使用通用分类搜索框展示任意任务维度，不内置严重度、难度或业务类别枚举。
