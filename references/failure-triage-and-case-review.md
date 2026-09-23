# 失败归因与用例人工审核

每轮评分完成后、创建问题集之前读取本文件。目标是先确认失败属于模型，而不是通过修改错误用例或降低标准制造虚假提升。

## 失败归因

宿主 Agent 逐条检查权威标签或输出定义、输入事实、完整上下文或明确观察窗口、相邻正反边界、字段来源、工具契约、Mock、轨迹和评分理由，写入 schemaVersion 3 的 `failure_triage.json`。文件必须嵌入本轮使用的任务级 taxonomy 快照；`failureOwner` 只能为：

- `model_failure`：用例正确，模型确实失败；只有高置信度此类用例进入问题集。
- `case_error`：用例事实、期望、参数或流程错误。
- `device_error`：运行器、Mock、脚本或轨迹异常。
- `scorer_error`：评分规则、解析或 Judge 错误。
- `spec_ambiguous`：需求本身无法唯一判断，必须询问用户。
- `tool_contract_error`：工具 Schema、描述和期望调用矛盾。

将最终归因快照保存在用例根目录内，审核文件会绑定它的 SHA-256。验证时必须同时提供本轮评分目录；存在修正登记表时一并提供，确保覆盖全部 `FAIL` 且不会重复修正内容未变化的已批准用例：

```bash
python bin/failure_triage.py --triage cases/failure-triage.json \
  --scores runs/demo/scores/baseline \
  --registry cases/case_corrections_registry.json --case-root cases
```

首次运行还没有登记表时省略 `--registry`，但仍保留 `--scores`。若没有失败用例，则无需创建归因和审核文件。

需要成批裁决人工标签时，先生成 `templates/case-adjudication.json`，由用户逐批确认后执行：

```bash
python bin/case_adjudication.py --decisions cases/case-adjudication.json \
  --adjudication-registry cases/case_adjudications_registry.json \
  --exclusion-registry cases/case_exclusions_registry.json
```

动作只允许 `keep-human`、`accept-model`、`corrected`、`exclude`。`exclude` 必须单独进入排除登记，保留来源、理由、审核人和时间；不得删除原始用例。`corrected` 仍须进入下述修正提案和人工审核哈希链，不能借裁决脚本直接改正式文件。

每条归因必须填写 `authoritativeBasis`、`contextScope`、`neighborCaseIds` 和 `decisionAnchor`。若使用任何业务、上下文或事件字段，还必须在 `fieldsUsed` 与 `fieldProvenance` 中逐项记录定义来源、生产者和写入/推导链路。字段与标签相关不代表字段定义就是该标签原因。

每条归因还必须填写：

- `caseStatus`：`active | pending_review | corrected | excluded`，描述用例生命周期，不与失败归属混用。
- `taxonomyValues`：只引用用户确认的任务级分类维度和等级。
- `stabilityEvidence`：记录 `validRuns`、`failureRuns`、`passRuns`、`invalidRuns` 和相同环境哈希。
- `stabilityLabel`：由 taxonomy 的 `stabilityPolicy` 根据原始计数解释；Skill 不提供统一高频/低频比例。
- `optimizationEligible`：只有高置信、active 的 `model_failure` 才能为 true。

稳定性尚未确认时应重复运行同一用例；覆盖缺口或缺少正反边界时才新增不同结构的用例。用例错误、评分错误和装置错误不能通过扩充同类用例解决。

## 用例修正提案

`case_error` 只能把修正版写入 `cases/proposed-corrections/`，正式用例保持不变；初始 `reviewStatus` 必须是 `pending_review`。向用户展示原用例、候选用例、逐字段差异、修正理由、评测口径变化和需要失效的历史结果。

用户可以批准、修改后批准、拒绝或暂不处理。只有用户明确确认后，宿主 Agent 才能写 `reviewedBy=user`、`reviewedAt`、`approvalStatement` 以及 `approved|modified_and_approved`。审核文件使用 schemaVersion 2，并写入 `triagePath` 与 `triageSha256`；Judge、配置模型和 headless 模式不能代替人工批准，headless 遇到待审核用例必须暂停。

## 应用与复用

```bash
python bin/case_correction_guard.py --review cases/case-correction-review.json \
  --case-root cases --receipt runs/demo/case-correction-receipt.json
```

应用器在写正式用例前一次性验证全部条目：归因文件哈希与 `case_error/pending_review` 提案链、人工审核状态、用户凭据、路径范围、原始哈希、候选哈希和用例 ID。任一条失败则全部不写入；写入途中出错会回滚已替换用例和元数据。应用前备份原用例，应用后更新 `cases/case_corrections_registry.json`。

已应用记录用 `caseId + correctedSha256` 识别：当前内容仍等于已批准哈希时直接复用，不重复审核；内容变化时旧批准失效，必须生成新提案并重新人工审核。

只要收据中 `requiresRebaseline=true`，旧轨迹、分数、失败集和基线均不再与新用例同口径。应用器同时写入 `.rebaseline-required.json`；下一次正式运行必须提供同一收据，并使用完整用例集建立全量基线：

```bash
python bin/runner.py --config config.json --task task.json \
  --prompt runs/demo/prompt_v1.md --cases cases/generated \
  --out runs/demo/trajectories-rebaseline --run-id rebaseline-v1 --run-kind full \
  --case-correction-receipt runs/demo/case-correction-receipt.json
```

Runner 会同时核对收据、待重建标记、当前用例集哈希、完整用例 ID 清单及数量；`--limit`、回归子集、缺失或过期收据都会失败。成功写入 manifest 后标记才变为 `completed`。旧结果只保留为历史。

裁决或排除改变了正式统计范围时，同样必须生成新的选集快照和基线；使用 `select_cases.py` 按明确 ID 构造本轮集合，并把 ID、数量和哈希写入 manifest。不得让旧分数与新裁决范围直接比较。

`--receipt` 是必填项，并与正式用例、登记表和待重建标记处于同一事务：回执写失败会整体回滚。若旧版本曾留下 pending 标记但回执丢失，可在用例和审核文件均未变化时再次运行应用命令，脚本会复用登记记录并重新生成 `requiresRebaseline=true` 的收据，不会重复修改用例。

本地 JSON 哈希链用于防止产物遗漏、串轮和审核后被改写，不提供操作系统级用户身份认证；`reviewedBy=user` 仍必须来自当前用户的明确确认。
