# 提示词优化报告 — <任务名>（<运行名>）

## 结论

- 最终版本：`<finalPrompt>`；工具版本：`<finalTools>`
- 最终版本决策：`accepted | production_best | experimental_best | baseline`；依据：<决策理由>
- 评分模式：`rubric | expected | expected-semantic | tool-binary`；对照方式：`deterministic | judge`；Judge 调用：N
- 运行范围：`full | regression | stability | candidate | selftest`
- 本结论来源：`run_manifest.json` 中的 `<cardRound>`，轨迹目录 `<trajectoryDir>`，分数目录 `<scoreDir>`

## 数据可信度

- 清单来源：`native | legacy-reconstructed`
- 提示词、工具、用例 SHA-256：<有则列出；历史缺失时明确写 snapshotUnavailable>
- 全部记录：N；有效用例：N；`INVALID`：N
- 轨迹与评分集合：一致/不一致；不一致时停止给出总体结论
- 装置异常：Mock缺失/耗尽、截断、停滞、评分失败及数量

## 模型画像与硬约束

- 被测模型：<provider> / <modelFamily> / <model>；思考模式：<开/关>
- 模型画像：<profileLabel>（<promptProfile>）；匹配置信度：<confidence>
- 画像选择：<用户已接受/修改后接受/未采用>；硬约束：N 条
- 硬约束验证：<PASS/FAIL/UNVERIFIABLE>；验证文件及 SHA-256：<path/hash>
- 运行时要求：<api/context/tool/guard 未满足项；没有则写无>

## 分数

普通/期望评分：

| 轮次 | 运行范围 | 有效用例 | INVALID | 平均分 | 通过率 |
|---|---|---:|---:|---:|---:|
| baseline | full | N | N | XX.X | XX.X% |

工具二值评分：

| 轮次 | 运行范围 | 有效用例 | correctCount | wrongCount | score |
|---|---|---:|---:|---:|---:|
| baseline | full | N | N | N | XX.X |

全量与回归子集分开呈现；子集只报告交集修复情况，不把子集均分写成总体提升。

## 轮次决策

| 轮次 | 基线 | 提示词/工具版本 | 范围 | 修复 | 退化 | 决策 | 理由 |
|---|---|---|---|---:|---:|---|---|
| <round> | <baseRound> | <version> | <kind> | N | N | accepted/rolled_back | <reason> |

## 失败分析

| 用例 | 分数 | 失败标签 | 工具调用 | 根因 | 下一步验证 |
|---|---:|---|---|---|---|
| <caseId> | 0 | tool_misuse | <实际/期望> | <根因> | <回归用例> |

## 优化总结

- 有效改动：<由证据支持的通用改动>
- 无效或回退改动：<版本及原因>
- 高频根因：<按失败标签和工具契约汇总>
- 剩余风险：<未覆盖场景、历史清单缺失、模型波动等>

## 单根因迭代证据

- 根因假设：<rootCauseHypothesis>
- 最小改动范围：<changeScope>
- 问题集：N 条；相邻正确集：N 条
- 重复验证：N 次；各轮修复率与新退化：<结果>
- 全量回归：<已完成/未完成>；随机波动：<已排除/仍不稳定>
- 架构轨道：`production-candidate | experimental`；实验架构不得替换生产最佳版本
- 正反边界：<boundaryPairs 及决定结论不同的结构证据>

## 成本与调用

| 阶段 | 运行ID | 用例范围 | 调用次数 | 估算 token |
|---|---|---:|---:|---:|
| 执行 | <id> | N | N | N |
| 评分 | <id> | N | N | N |

## 附录

- 最终提示词全文：`<finalPrompt>`
- 最终工具定义：`<finalTools>`
- 清单：`run_manifest.json`
- 轮次决策日志：`rounds.json` 或 manifest 指定文件
