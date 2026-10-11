# 公开示例：意图分类的失败定位与离线评分

[English](README.en.md) · [返回仓库首页](../../README.md)

这个示例让你先体验确定性评分、失败定位与无效运行排除。所有输入和输出轨迹都是人工编写的公开教学数据，没有客户资料或 API Key；**没有运行真实模型，也没有证明候选提示词效果更好**。

## 原始任务与待验证候选

[原始提示词](prompt_v1.md)要求返回四种意图之一，但没有定义取消和退款重叠时怎么选择。示例业务口径规定：用户明确要求退回已付款项时，返回 `refund`，即使同时要求取消订单。

[候选提示词](prompt_v2.md)补充了这条边界规则。它仅是待验证假设，不是已接受版本。实际使用时，需要用生产模型执行两份提示词，再做重复验证和同集回归。

## 数据与预制轨迹

[dataset.json](dataset.json)保存用户输入、分类与期望标签；[recordings/](recordings/)保存人为构造的输出和异常，用来解释评分器行为。

| 用例 | 期望输出 | 预制轨迹 | 评分应如何解释 |
|---|---|---|---|
| 咨询营业时间 | consultation | consultation | PASS |
| 明确退款 | refund | refund | PASS |
| 同时取消和退款 | refund | cancel | FAIL，分类边界没有满足 |
| 普通感谢 | other | 模拟执行超时 | INVALID，装置异常不计模型失败 |

## 在仓库根目录运行

只需要 Python 3，使用仓库现有评分器，无须配置模型：

```bash
python -c "import shutil; shutil.copytree('examples/intent-classification/recordings', '.demo-output/recordings', dirs_exist_ok=True)"
cd .demo-output
python ../bin/scorer.py --config ../examples/intent-classification/config.offline.json --task ../examples/intent-classification/task.json --cases ../examples/intent-classification/dataset.json --trajectories recordings --out scores/offline --manifest run_manifest.json
cd ..
```

查看 `.demo-output/scores/offline/_summary.json` 和逐例 JSON。4 条预制轨迹中，有效评分 3 条，PASS 2 条、FAIL 1 条、INVALID 1 条，预期有效通过率为 66.7%，Judge 调用为 0。这是人工轨迹的评分演示结果，不能当作模型基线或优化提升。

生成 HTML 评分报告：

```bash
python bin/report_gen.py .demo-output --rounds offline --card-round offline --trajectories recordings --out .demo-output/report.html --user-requested
```

用浏览器打开 `.demo-output/report.html` 查看逐例分数与失败。报告不指定最终提示词，因为本示例没有执行候选或作出接受决定。生成文件保留在被 Git 忽略的 `.demo-output/` 中。

本仓库附有[实际执行评分器得到的摘要](offline-result.json)，便于核对复现结果。摘要只描述人工预制轨迹。

## 接入真实模型后怎么验证

将数据、提示词和通用运行脚本复制到独立任务工作区。配置自己的生产模型，向 Claude Code 或 Codex 发起：

```text
使用 prompt-optimizer 优化意图分类提示词。以公开示例的 prompt_v1.md 为初稿，
dataset.json 为待审核种子用例，prompt_v2.md 为待验证候选。工作区使用独立目录。
先确认生产模型、取消/退款边界、预算和成功标准，再执行模型调用。
使用 expected 确定性评分；不调用 Judge；基线和候选使用相同模型配置及用例。
```

这 4 条数据用于教学，不能代替完整覆盖。真实评测至少需要补充仅取消、否定退款、历史退款提及、重叠诉求和正常咨询等保护用例；覆盖范围由业务决定。修正标签后重建基线，并检查候选是否修复问题、是否误伤保护集、重复运行是否稳定，最后才决定接受或回退。
