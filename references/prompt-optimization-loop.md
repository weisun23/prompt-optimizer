# 评分、回归与优化循环

进入评分、优化、版本选择或报告阶段前读取本文件；评分文件和 `run_manifest.json` 的字段定义同时遵循 [scoring-and-manifest.md](scoring-and-manifest.md)。

## 评分模式

### judge：多维度评分

```bash
python <workspace>/bin/scorer.py --config <config.json> --task <task.json> \
  --trajectories <trajectories-dir> --cases <cases> --out <scores/round-id>
```

使用 `prompts/judge_score.md`。若关闭 judge 模型思考且需要更稳的显式推理，加 `--judge-steps` 使用 `judge_score_step.md`。步骤模板先输出 5 个编号步骤，最后输出一个 JSON；JSON 后不得有文字。

每条可评分多次：两次取平均，三次及以上取中位数；分差达到任务阈值时补评一次。单次解析失败可重试一次，失败轮不计聚合；全部失败才标记 `parse_error`。

### expected：程序确定性对照

分类、提槽、固定 JSON 和结构化抽取默认使用 `bin/scorer.py` 的确定性路径，不配置 Judge。文本标签按配置标准化后精确比较；结构化结果解析为严格 JSON 后递归比较。完全一致为 100/PASS，否则为 0/FAIL；缺少 `expectedOutput` 为 `INVALID/case_error`。每条 `judgeTimes=0`、`judgeCalled=false`，不做重复评分或分歧补评。

### expected-semantic：语义期望对照

只有同义表达、自由文本重排或业务容差无法可靠程序化时，才显式选择 `expected-semantic`，使用 `prompts/expected_check.md` 和 Judge。逐项输出 `matchLevel=exact|partial|wrong`，最终 verdict 仍只有 PASS/FAIL；不要把 PARTIAL 当第三种 verdict。

### tool-binary：工具二值评分

```bash
python <workspace>/bin/score_tool_binary.py --config <config.json> --task <task.json> \
  --trajectories <trajectories-dir> --cases <cases> --out <scores/round-id>
```

模板位于 `prompts/tool_binary_score.md`。只判断函数名、次数、顺序、时机和参数。每条有效用例只能 0 或 100，同时输出统一字段 `totalScore/verdict/scoreMode` 和兼容字段 `score/correctCount/wrongCount`。

## 事实与装置边界

- 用例、variables、scriptedExact、工具返回和运行时事实快照优先于模型自由叙述。
- `toolsExpect` 为空但模型调用工具，必须记录 `unexpected_tool_call`，通常归为 `tool_misuse`。
- 已知函数的错误调用不是缺 Mock 导致的装置错误。
- 缺失或耗尽必需 Mock、脚本无法推进、轨迹缺失等装置问题标记 `INVALID/device_error`。
- INVALID 的 `totalScore` 为 null，不进入均分、通过率、失败集、修复率或退化率。

## 失败集与回归顺序

1. 评分后先按 [failure-triage-and-case-review.md](failure-triage-and-case-review.md) 归因；只有高置信度 `model_failure` 写入 `cases/regress.json`。
2. `case_error` 必须人工审核后才能应用；应用修正后用例集、旧轨迹、旧分数和旧失败集失效，携带修正收据用完整用例集重建 `full` 基线。
3. 新候选先只跑失败集，使用独立 `trajectories-regressN` 与 `scores/regressN`。
4. 候选未达到任务级 `acceptancePolicy` 时回退并继续下一个根因，不浪费全量调用。
5. 候选满足任务级策略后，用与基线相同的完整用例集跑全量。
6. 回归子集和全量属于不同统计口径，不得把两者均分直接画成连续提升曲线。

失败候选不结束整个优化任务：记录根因、改动、问题集/保护集结果和回退原因后，选择下一个仍有证据的根因继续。固定候选轮数不是停止条件。

## 版本、实验账本与恢复摘要

每次实际修改提示词前先创建独立候选，不得覆盖旧版本：

```bash
python <workspace>/bin/optimization_journal.py start --run <run-dir> \
  --parent <prompt_vN.md> --plan <optimization_iteration.json> \
  --change-summary <本轮准备验证的方向>
```

脚本复制父版本为下一个 `prompt_vN.md`，并向 `rounds.json` 追加 `attempt-started` 事件。若工具定义也要修改，增加 `--parent-tools <tools_vN.json>`，生成与候选提示词编号一致的工具副本。

- 已存在的提示词和工具版本禁止覆盖；失败、回退和实验版本同样保留。
- 只做重复运行、稳定性抽检或更换运行范围时复用同一提示词版本，通过新的 invocation/round ID 区分，不创建空版本。
- 只修正用例或重建基线时记录里程碑和新的用例快照，不创建提示词版本。
- 最大版本号只表示创建顺序，不表示最优或最终版本。

候选得出结论后追加完成事件：

```bash
python <workspace>/bin/optimization_journal.py complete --run <run-dir> \
  --attempt-id <attempt-NNN> --decision <accepted|rolled_back|continue|inconclusive|experimental> \
  --conclusion <本轮最终结论> --next-action <下一步> \
  --round <对应评分轮> --evidence <JSON字符串或JSON文件>
```

`rounds.json` 是追加式实验账本，必须保留父版本、根因、改动方向、问题集、保护集、评分轮、决策、结论和下一步。`optimization-state.json` 保存当前最佳版本、活动候选、已验证结论、已否决方向和下一动作。

基线重建、人工用例修正、统计口径改变、根因平台期、暂停或交接使用：

```bash
python <workspace>/bin/optimization_journal.py milestone --run <run-dir> \
  --type <baseline|case-correction|scope-change|plateau|handoff> \
  --conclusion <新进展或口径变化> --next-action <下一步>
```

脚本会从账本和当前状态生成 `OPTIMIZATION_SUMMARY.md`。它是长上下文压缩、任务暂停和交接时的恢复入口，不是新的事实源；分数、模型、用例集合和文件版本仍以 manifest、评分文件及哈希为准。继续任务时先读摘要，再核验权威产物。

## 全局单根因迭代

所有任务强制使用 `evidence-driven-single-hypothesis`：

```text
校正用例 → 单根因最小改动 → 问题集与相邻正确集重复验证 → 全量回归 → 排除随机波动
```

用例契约一旦修改，旧轨迹和分数不能继续作为基线。每轮从失败聚类中只选择一个根因假设，写入 schemaVersion 3 的 `optimization_iteration.json`：

- `rootCauseHypothesis`：本轮唯一假设。
- `architectureTrack`：`production-candidate` 才能进入接受门禁；结构重构恢复期使用 `experimental`。
- `changeScope`：允许修改的章节、规则或工具描述。
- `requirementIntegration`：新增需求整合分析。先记录检查过的章节、已有权威规则和共享决策维度，再从 `merge-existing`、`generalize-replace`、`new-local-rule`、`structural-refactor` 中选择策略。
- `problemCaseIds`：该根因对应的失败集。
- `neighborPassCaseIds`：边界附近原本正确、可能被误伤的用例。
- `boundaryPairs`：问题用例、表象相似但结论不同的相邻正确用例，以及决定二者不同的结构证据。
- `repeatRuns`：候选稳定性通常验证 3 次；降为 2 时必须说明稳定性证据。这是同一候选的重复次数，不是优化总轮数。
- `unstableRepeatRuns`：边界曾出现方向不一致时通常追加到 5 次；仍不稳定则继续增加或判定证据不足，不得把 5 次当成保证收敛的上限。
- `acceptancePolicy`：用户或正式任务契约确认的成功标准、关键用例和保护用例。禁止使用通用80%修复率、60分及格线或固定退化数量。

整合策略约束：

- `merge-existing`：把新条件并入已有权威规则，不增加平行规则。
- `generalize-replace`：建立更通用的规则，并列出、删除或替换已被覆盖的旧表达。
- `new-local-rule`：只有检查过的现有规则都无法承载时使用，必须写 `newRuleJustification`，并放入对应职责章节。
- `structural-refactor`：仅与 `changeMode=whole-rewrite` 配合，处理多处权威规则冲突。

禁止新增顶部补丁块、临时注意事项、案例专用规则或与现有规则近义的平行约束。脚本不使用固定字符增长阈值，而是要求明确记录结构净变化并验证修改位置。

执行修改前：

```bash
python <workspace>/bin/iteration_guard.py --plan <optimization_iteration.json>
```

问题集和相邻正确集必须使用同一候选分别重复运行。结果记录 `hadInitialInstability`；为 true 时问题集和相邻正确集都必须达到 `unstableRepeatRuns`。确定性任务逐条核对用户列为关键的失败与保护 ID，其他容许范围按任务策略判断；随机任务按用户目标、成对重复证据和严重度判断。未满足任务级策略或轮间方向不一致时不能进入全量。全量完成后把结果写入迭代结果 JSON，再执行：

```bash
python <workspace>/bin/iteration_guard.py --plan <optimization_iteration.json> --results <iteration_results.json>
```

只有退出码 0 才能记录 `accepted`。确定性任务要求用户确认的关键用例与保护用例逐条通过，并达到本任务 `successCriteria`；不得把全部非关键问题集强制为另一个通用 100% 门槛。随机任务使用用户目标、成对重复证据和严重度判断。整体重写仅在多处权威定义互相冲突等结构性根因下使用，并必须填写 `rewriteJustification`；仍执行全部门禁。首版重构即使低于生产版本也可标为 `experimental` 留存并继续按单根因恢复，但不能标为 `accepted`、`production_best` 或替换最终版本。

## 证据与决策架构

- 先核对权威标签、输出契约和完整上下文，再判断是模型失败还是用例错误。
- 优先使用角色、顺序、状态、事件和字段关系；关键词仅在契约明确规定时作为决定性证据。
- 使用字段前记录定义来源、生产者、写入时机和读取/推导链路，不按样本相关性解释字段。
- 涉及时序先定位首次或决定性事件，并明确事件前后或完整观察窗口。
- 分类、路由和状态机任务可先处理强事件例外，再按稳定维度分区、限制分区内候选，最后设置短局部优先级。
- 每个规则使用正反边界成对验证，示例描述通用结构，不复制失败用例原文。
- 用 `root-cause-analysis.json` 记录最早出现预期/实际分歧的阶段、证据及下游影响，并通过单变量消融验证关键猜测；不得只看最终错标签就直接改末端规则。
- 先确认输入事实对 candidate 真正可见。仅存在于 `scenario`、生成计划或未发送 `flowGuide` 的事实不能解释为模型失误。
- 工具用例分别声明立即调用、允许澄清后调用和整通最终调用；不得把“晚一轮但最终正确”统一误判为漏调。
- 固定 JSON 用字段级指标定位局部退化；总体正确率不能替代起始/结束字段、槽位或标签分区的独立证据。

## 优化输入包

若 candidate 命中模型画像，先按 [model-profile-adaptation.md](model-profile-adaptation.md) 展示强烈推荐并取得用户确认。接受项写入版本化 `optimization_constraints.json`；拒绝项不得暗中应用。

在画像确认之前，先逐项确认通用优化原则。通用原则与画像指令分开保存、合并验证；`PRINCIPLE-CONCISE` 未选中时禁止使用 `check_prompt.py --allow-shrink`。

填充 `prompts/optimize.md` 时不得缺少：

- 当前提示词和当前已接受版本
- 当前 tools 定义及上轮 tools 改动
- scoreMode、runKind、caseSetScope 和 manifest 摘要
- 分数汇总、失败用例完整轨迹、实际工具调用与返回
- 已启用 constraints、上轮提示词改动、回退历史
- 本轮 optimizationStrategy
- 模型画像、用户确认的 hardConstraints、runtimeRequirements 和上轮 constraintValidation

先判断失败来源：

1. 装置/manifest/评分协议错误：先修装置，不改被测提示词。
2. 用例契约错误：修用例并生成新快照，旧分数不可沿用。
3. 提示词行为错误：修改 system/user 模板。
4. 工具描述错误：可修改 description、参数说明、枚举和 required 标注；函数名与参数名默认不改。

text-protocol 的问题分别定位到：决策话术、提槽话术、工具描述，不要用一条业务补丁同时掩盖三处。

## 优化策略与输出

- **单根因最小改动（默认）**：修改一个或多个必要位置，但全部必须服务于同一个根因假设；输出修改块并机械应用。
- **整体重写（例外）**：只有结构断裂、职责混乱或权威规则互相冲突时使用，必须给出 `rewriteJustification` 和保留契约清单。

无论哪种策略，都必须保留用户未授权变更的占位符、功能、输出格式、函数名和参数名。

## 改动校验

新版本落盘后立即运行：

```bash
python <workspace>/bin/check_prompt.py --old <prompt-old.md> --new <prompt-new.md> \
  --old-tools <tools-old.json> --new-tools <tools-new.json> \
  --constraints <optimization_constraints.json> \
  --iteration-plan <optimization_iteration.json> \
  --capability-inventory <capability-inventory.json> \
  --architecture-track <production-candidate|experimental>
```

只有用户启用“提示词精简”约束时才允许 `--allow-shrink`。退出码非 0 时先修复再重跑，不得带失败校验进入执行。

校验关注：占位符集合、内容异常缩减、需求整合位置、结构净变化、关键章节、函数名/参数名和 diff 摘要。字符或标题增长只作为证据展示，不使用通用比例自动拒绝。

存在硬约束时还必须运行：

```bash
python <workspace>/bin/check_constraints.py --config <config.json> \
  --constraints <optimization_constraints.json> --prompt <prompt-new.md> \
  --compliance <constraint_compliance.json> --out <constraint_validation.json>
```

优化器的 compliance 声明不能替代独立核对。任何 `FAIL` 或 `UNVERIFIABLE` 都必须打回；验证文件中的提示词和约束哈希必须与 runner 输入一致。

## 轮次比较与版本决策

```bash
python <workspace>/bin/compare_rounds.py --run <run-dir> \
  --rounds <baseline>,<candidate> --task <task.json> --record \
  --version <prompt-version> --decision <accepted|rolled_back> \
  --reason <中文理由>
```

- 默认要求两轮有效用例 ID 完全一致。
- 子集比较必须显式 `--allow-subset`，只比较交集，并展示双方和交集数量。
- 查看均分、通过率、分类变化、FAIL→PASS 修复、PASS→FAIL 退化和 INVALID 变化。
- judge 单次波动可有噪声；小幅均分变化不能脱离用例级证据单独决定接受。
- 接受条件只来自本任务 `acceptance-policy.json`：达到用户确认的成功标准、关键与保护用例满足约定、重复结果稳定、同口径全量无用户定义的不可接受退化。修复率只作为证据，不自带通用阈值。
- 未满足本任务策略则拒绝或回退到上一个已接受版本，更新 `optimization-state.json` 后继续验证其他根因或假设。
- 使用 `--record` 写 `rounds.json`；最终版本必须来自决策日志或 manifest，不按最大编号推断。

多个候选时，每个候选先跑同一回归集，再让最优候选跑全量。不得让不同用例集的候选直接竞争总体均分。

## 停止条件

满足任一条件停止：

- 用户明确中断
- 用户明确设置的调用、费用或时间资源预算耗尽
- 所有可归因失败均已处理，且没有新证据、可测试假设或其他可行动根因
- 模型/API 或装置持续异常使结果不可判定

候选失败不属于停止条件。自动回退后写简短迭代摘要，并继续下一个根因。交互模式只在业务口径、人工用例裁决、预算扩展或新权限确实需要用户决定时暂停，不要求用户每轮回复“继续”。

达到目标、用户停止、资源预算耗尽、确认没有可验证方向或准备交接时，先记录对应 milestone 并刷新 `OPTIMIZATION_SUMMARY.md`，确保下一次继续时不依赖聊天上下文。

## 报告

正式报告不是每轮产物。`CANDIDATE_REJECTED_CONTINUE`、局部门禁失败或全量回退只记录迭代摘要，不调用报告生成器。

`OPTIMIZATION_SUMMARY.md` 是持续更新的恢复摘要，不等于正式报告；失败候选更新恢复摘要不违反报告门禁。

```bash
python <workspace>/bin/report_gen.py <run-dir> --summary <summary.txt> \
  --workflow-status ACCEPTED
```

有 manifest 时，只消费 `includeInReport=true` 的 invocation，并用 `cardRound`、`finalPrompt`、`finalTools` 和 decision log。旧运行有多个目录时，显式给出：

```bash
python <workspace>/bin/report_gen.py <run-dir> \
  --rounds <round-a>,<round-b> --card-round <round-b> \
  --trajectories <trajectory-dir> --final-version <prompt-file> --user-requested
```

报告必须：

- 区分全量、回归子集、稳定性抽检、候选验证和自检。
- 显示评分模式、有效用例数、INVALID 数和统计口径。
- 二值评分显示 `correctCount/wrongCount`；不得读取 `_summary.json` 为用例。
- 轨迹为 0 或卡片轮评分/轨迹 ID 不一致时失败。
- 最终版本展示决策理由；回退版本不能因编号更大而被展示为最终版。
- Markdown 与 HTML 的结论使用同一数据源。
- HTML 提供失败筛选、函数/用例搜索、版本与目录说明、分页和移动端布局。

只有 `ACCEPTED` 或用户明确要求时，正式报告才写入 `reports/<task-name>/<run-name>/report.md` 与 `report.html`。失败轮继续保留 manifest、评分和回退摘要，不生成正式报告。
