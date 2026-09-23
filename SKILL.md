---
name: prompt-optimizer
description: 当用户需要优化、评测或迭代提示词、系统话术、工具调用规则，或需要生成测试用例、模拟多轮对话、评分、回归和比较提示词版本时使用。
---

# 提示词优化 Agent

把提示词视为可测试程序：建立可复现基线，生成覆盖关键风险的用例，执行并评分，依据失败轨迹优化，再用同口径回归验证。

## 先判断入口

- **新建任务**：缺少 `task.json`、`config.json` 或可识别的运行目录。集中确认一次配置，再开始执行。
- **继续运行**：已有任务和运行产物。先读 `OPTIMIZATION_SUMMARY.md` 了解恢复入口，再用 `run_manifest.json`、`rounds.json`、`optimization-state.json`、`task.json` 和 `config.json` 核对事实与哈希；只确认发生变化的模型、提示词、工具、用例范围、预算或模式。
- 旧运行没有 manifest 时，必须让用户明确本轮提示词、工具、用例、轨迹、分数目录和运行类型，再补建 manifest。不得根据目录名或最大版本号猜测。

## 模板与项目边界

Skill 的 `bin/`、`prompts/`、`profiles/` 是通用模板，不是项目运行时依赖。

1. 初始化时复制到 `<workspace>/bin`、`<workspace>/prompts` 与 `<workspace>/profiles`；项目之后只使用并可修改自己的副本。
2. 写入 `<workspace>/.skill-source.json`，记录 Skill 版本和同步日期。
3. 工作区已存在副本时不得覆盖。版本不同时先列出差异，由用户决定是否同步。
4. Skill 主目录保持通用，不写入项目业务规则、模型密钥或运行数据。

完整的目录、配置、用例、模拟用户、工具 Mock、自检和运行命令契约见 [references/execution-contract.md](references/execution-contract.md)。

评分协议、manifest、回归、优化、接受/回退与报告规则见 [references/prompt-optimization-loop.md](references/prompt-optimization-loop.md) 和 [references/scoring-and-manifest.md](references/scoring-and-manifest.md)。执行对应阶段前必须读取相应参考文档。

每轮评分后、形成问题集前必须读取 [references/failure-triage-and-case-review.md](references/failure-triage-and-case-review.md)，先归因失败并完成人工用例审核门禁。

被测模型涉及工具调用或用户要求按厂商/模型习惯优化时，先读取 [references/model-profile-adaptation.md](references/model-profile-adaptation.md)，解析模型画像并完成用户确认。

## 角色边界

- **宿主 Agent 是唯一优化执行者**：当前 Codex/Claude 必须亲自读取失败证据、应用用户确认的模型画像指令并修改提示词；配置模型不负责修改提示词。
- 模拟用户、被测模型、语义评分模型和可选提槽模型在对应阶段启用时使用用户配置的 API。确定性程序评分不配置、不调用评分模型；当前宿主 Agent 不得静默扮演启用的模型角色。
- 当前宿主 Agent 负责：配置审查、用例审查、运行编排、失败根因判断、提示词或工具描述优化、版本决策和汇总。
- 确定性工作必须调用工作区脚本；不得手工替代执行、评分、比较、manifest 或报告生成。
- 模型配置缺失时列出缺项并等待用户补齐；不得猜模型、地址、密钥或自行试探替代服务。

## 对用户一律用中文说明

内部枚举可以写入 JSON，但向用户展示时使用中文名称并解释含义：

| 内部值 | 中文说明 |
|---|---|
| `chat` / `inject` | 对话型 / 输入注入型 |
| `judge` | 多维度评分 |
| `expected` | 程序确定性对照，固定结果任务每条输出 0 或 100，不调用 Judge |
| `expected-semantic` | Judge 判断期望输出与实际输出的语义等价 |
| `tool-binary` | 工具调用二值评分，每条只能 0 或 100 |
| `native` / `text-protocol` | 原生函数调用 / 文本函数名协议 |
| `interactive` / `headless` | 关键节点确认 / 无人值守 |
| `full` / `regression` / `stability` / `candidate` / `selftest` | 全量 / 失败集回归 / 稳定性抽检 / 候选验证 / 装置自检 |
| `INVALID` | 测试装置无效，不计入均分、通过率和失败集 |

## 新建任务初始化

### 1. 定位工作区与任务

- 确认 workspace、任务名和用户提供的初始 system 提示词、user 模板或文件路径。
- 用户未提供提示词时，只能询问来源，或在用户明确选择“冷启动”后根据其业务背景、期望行为、输出要求和约束生成草稿并请其确认。
- 不得到其他任务、示例或旧运行中拼凑初始提示词。

### 2. 判断任务形态

- `userTemplate` 非空通常为 `inject`；为空时依据任务描述和初始提示词判断 `chat` 或 `inject`。
- `userInputTemplate` 只约束对话型用户消息，不用于判定任务形态。
- `evalMode=auto` 时，分类、提槽、固定 JSON、结构化抽取使用 `expected`；其他 inject 使用 `expected-semantic`，chat 使用 `judge`；工具专项可以选 `tool-binary`。
- 拿不准时询问用户；无人值守模式可暂按 chat，但必须在报告中说明推断。

### 3. 一次性确认配置

把以下内容合并为一张中文总览，用户一次确认或集中修改：

- 先运行 `bin/evaluation_mode.py --task <task.json>`。当 `scoringJudgeRequired=false` 时，结果评分配置中不得展示 judge，且评分次数显示为程序判断 1 次、Judge 调用 0 次。

- 先按阶段列出实际需要的模型角色，再展示其模型名、厂商、模型族、API 风格、地址来源、温度和思考开关；密钥只说明来源，不显示值。candidate 必选；generate 仅生成用例时需要；simulator 仅自由/引导式对话需要；slotfiller 仅 text-protocol 提槽需要；judge 仅 `judge`、`expected-semantic`、含 `semanticToolArgFields` 的 `tool-binary` 或语义硬约束验证需要。
- `optimizationTargetRole`（本次由宿主 Agent 优化哪个角色的提示词，默认 candidate）和 `promptTaskType`；目标角色的模型画像、匹配依据、置信度、工具选择策略和运行时能力。不得根据 `base_url` 猜厂商。
- 并发、生成批量、用户明确的调用/费用/时间资源预算、运行模式和语言。不得设置默认优化总轮次、默认用例总上限或每类固定数量。只有使用 Judge 时才确认每用例评分次数；`expected` 固定为程序判断一次，Judge 调用 0 次。
- 任务形态、评分模式与维度、优化策略、覆盖项、模拟用户模式、候选数、最大对话轮数、工具模式、Mock 策略、终止工具、变量和报告触发方式。用例分类维度、等级数量和稳定性标签由当前任务提出并经用户确认，不使用 Skill 固定的严重度或难度枚举。正式报告只在成功或用户明确要求时生成。
- 通用优化原则逐项展示并由用户确认：全局审视、不乱打补丁、提示词精简、结构证据优先、字段来源可追溯、时间锚点与观察窗口、正反边界成对验证、变量占位符不可动、不删除既有功能、失败根因优先、保持原格式、函数名参数名不可动。其中全局审视必须包含“新增需求先查找并优先合并现有权威规则”；不得用一句“沿用默认”代替展示。

推荐但不替用户决定：

- generate 开思考；simulator 与 slotfiller 关思考；candidate 与生产一致。
- 长轨迹或复杂 judge 评分推荐开思考；快速迭代可关思考并用 `--judge-steps`。
- `--judge-steps` 只切换可见步骤模板，不改变模型思考开关。
- 确定性任务不展示 judge 供用户选择，也不展示 Judge 温度、思考开关或评分次数；若用户需要语义容差，改为明确选择 `expected-semantic`。
- 若确定性任务仅因 `check_constraints.py` 需要模型，应明确标为“约束验证模型”，不能称为结果评分模型，也不参与结果分数或评分次数。

模型画像推荐：

- 使用 `bin/model_profiles.py --config <cfg> --task <task.json> --role <optimizationTargetRole>` 解析优化目标；candidate、slotfiller、judge、simulator、generate 都可以是目标角色。命中专用画像时，宿主 Agent 向用户显示“强烈推荐”、适合方式、不适合方式和已过滤的运行时要求。
- 画像中的 `hostOptimizationDirectives` 只由宿主 Agent 消费，不直接发给任何配置模型。配置模型不负责修改提示词。
- 模型适配覆盖全部提示词类型：先应用模型通用优化指令，再叠加分类、提槽、固定 JSON、抽取、总结、RAG、生成、Judge、模拟用户、普通对话或工具 Agent 的任务指令，最后叠加模型与任务的专用指令。
- `runtimeRequirements` 必须同时按目标角色和 `promptTaskType` 过滤；函数调用、语音 Guard 等要求不得进入豆包提槽或其他无关任务。
- 推荐不等于授权。用户必须选择全部接受、修改后接受或不采用；只有用户确认的规则成为硬约束。
- 用户接受或修改后，用 `bin/profile_constraints.py` 生成版本化的 `optimization_constraints.json`。用户文本优先于画像默认值。
- 工具型语音任务还要确认 `preambleOwner=model|host`，不得让模型和宿主同时负责垫词。

通用优化原则与模型画像并列存在：

- 通用原则是用户对本次优化方法的选择；模型画像是 Skill 针对目标模型和任务给出的强推荐，二者不能互相替代。
- 用户确认后，`profile_constraints.py` 将通用原则和已接受的模型/任务指令合并写入 schemaVersion 4 的 `optimization_constraints.json`，同时保留各自来源。schemaVersion 1/2/3 必须重新展示并确认新增原则后迁移，不能静默补默认值。
- 用户未确认通用原则、选择结果与硬约束不一致，或旧快照缺少确认记录时，验证必须失败。
- 只有用户选中“提示词精简”时，`check_prompt.py --allow-shrink` 才能与 `--constraints` 一起使用。是否丢失能力以 `capability-inventory.json` 为硬门禁；若需要长度比例，只能使用用户明确确认的 `--shrink-ratio`，不得套通用 75%。

## 全局优化纪律（强制）

所有提示词类型默认采用“校正用例 → 单根因最小改动 → 问题集与相邻正确集重复验证 → 全量回归 → 排除随机波动”。这是工作流门禁，不是可取消的画像建议。

- 每轮先用 `templates/optimization-iteration.json` 写 schemaVersion 3 的 `optimization_iteration.json`，只允许一个 `rootCauseHypothesis` 和一个最小可归因 `changeScope`。
- 每个新增需求必须先完成 `requirementIntegration` 需求整合分析：检查相关章节和权威规则，识别共享决策维度，优先合并或泛化替换。只有确认无法合并时才能使用 `new-local-rule`，并说明理由；禁止新增顶部补丁块、临时注意事项或案例专用规则。
- `bin/iteration_guard.py --plan <file>` 通过后才允许改提示词。一次可以同步修改多个位置，但它们必须服务于同一个根因；不得顺手修其他问题。
- 问题集必须与容易被误伤的相邻正确集组成明确 `boundaryPairs`，写出决定结论不同的结构差异；示例不得复制评测原文或偶然值。
- 问题集与相邻正确集通常重复验证 3 次；只有明确证明输出稳定时才能降为 2 次并写 `repeatRunsJustification`。任一边界曾出现方向不一致时通常追加到 5 次，仍不稳定则继续追加或判定证据不足。这里是候选稳定性验证次数，不是优化总轮数。
- 所有任务优先使用结构证据并核对字段来源；涉及时序先定位决定性事件和观察窗口。分类、路由和状态机任务再使用分区、候选标签限制及分区内局部优先级。
- 稳定后必须跑同口径全量。用 `bin/iteration_guard.py --plan <file> --results <file>` 验证接受证据，失败则回退。
- 整体重写只用于结构性根因并填写 `rewriteJustification`；首版可保留为实验架构继续局部恢复，但通过全部门禁前不得替换生产最佳版本。
- 创建 `coverage-plan.json`、`acceptance-policy.json`、`capability-inventory.json`、`baseline-selection.json` 和 `optimization-state.json`。用例数量由覆盖完成度决定，接受标准来自任务与用户，基线必须来自同模型配置、同生产模式和同用例集的证据。
- 批量生成用例前，先用 `prompts/case_taxonomy.md` 生成任务级 `case-taxonomy.json`，向用户展示分类维度、等级定义、归类规则和稳定性解释口径；确认后计算哈希并冻结。业务场景、影响等级、难度或稳定性频率不得由 Skill 写死。
- 每次实际修改提示词之前，必须用 `optimization_journal.py start` 从当前父版本创建新的 `prompt_vN.md`；禁止覆盖已有版本。只做重复运行或稳定性抽检时复用同一提示词版本并使用新的 invocation ID，不虚增版本。
- 每次候选得出结论后用 `optimization_journal.py complete` 追加尝试方向、证据、决策、结论和下一步；不得删除失败或回退记录。基线重建、人工用例修正、平台期、暂停和交接使用 `milestone` 记录。
- 产生新结论、达到目标、用户暂停、预算耗尽或准备交接时，必须更新 `OPTIMIZATION_SUMMARY.md`。该摘要只作为恢复入口，运行事实仍以 manifest、评分文件和哈希为准。
- 持续优化直到成功、用户停止、明确资源预算耗尽或确无可验证根因。候选失败后自动回退、写简短迭代摘要并继续下一个根因；不得因固定三轮或其他固定次数结束整个任务。
- 失败候选不生成正式报告。只有状态为 `ACCEPTED` 或用户明确要求时才调用 `report_gen.py`。

### 4. 初始化与连通检查

- 按执行契约复制模板和画像、写 `.skill-source.json`、创建任务和运行目录。
- 用用户确认的 candidate 配置做一次最小连通测试。失败时报告原始错误并提示检查配置；不得自行换模型或反复试探。
- 生成任务分析与场景计划，展示覆盖分类、数量和预计调用成本，用户确认后再批量生成。

## 继续运行

1. 先读取 `OPTIMIZATION_SUMMARY.md`，再读取 manifest、实验账本、当前状态、任务、配置、已接受版本、回退记录、`optimization_constraints.json`、约束验证和剩余预算。
2. 验证 manifest 中提示词、工具、用例哈希与当前文件；不一致时说明变化来源。
3. 展示当前阶段、统计口径和变化项，只请求一次确认。
4. 从已有产物继续，不重复已完成步骤，不覆盖 `_run_info.json` 或历史轮次。

## 标准流程

### 阶段范围（由宿主 Agent 编排）

新建任务默认执行完整流程；继续运行可只执行用户需要且前置产物齐全的阶段，例如只评分已有轨迹、只重跑执行、只回归优化或只重新生成报告。阶段选择不是某个脚本的通用 CLI 参数；宿主 Agent 应调用对应脚本，并在 manifest 中记录实际范围。跳过阶段前先验证依赖文件与哈希，不能用旧轨迹配新提示词评分。

### 步骤 0：任务分析

先用 `prompts/case_taxonomy.md` 根据任务契约提出动态分类体系并取得用户确认，再用 `prompts/task_analysis.md` 填充任务、原始提示词、分类体系、评分模式、工具、终止工具和既有失败，输出覆盖计划。使用 `baseline_guard.py` 在同一生产模型配置和用例集下选择证据最好的基线，不按文件时间或最大编号猜测；再建立能力覆盖清单和用户确认的接受策略。

### 步骤 1：用例生成与审查

用 `bin/case_taxonomy.py` 校验分类体系并取得 SHA-256，再用 `bin/gen_cases.py --taxonomy <case-taxonomy.json> --coverage-plan <coverage-plan.json>` 和 `prompts/gen_cases.md` 生成用例；场景计划与每条用例的 `taxonomyValues` 必须引用已确认分类体系。批量执行前审查：

- `toolsExpect` 调用次数与 `expectedToolArgs` 参数组数量一致。
- 顺序 Mock 数量足够，`terminalTools` 终止工具位于合理位置。
- `scriptedExact` 锁定关键事实，脚本能提供所有期望参数。
- 错误调用不需要预置 Mock；已知工具的错误调用应记录为模型缺陷，而非装置错误。
- 所有预期前置条件必须写入 `modelVisibleEvidence` 并真实存在于 system、实际消息、变量或工具结果；这是模型可见证据门禁，`scenario` 和未发送的 `flowGuide` 不能替代。
- 输入、业务字段、提示词内嵌标签或人工标签可能冲突时，先在 task 中声明 `evaluationSourcePrecedence`；用例用 `expectedOutputSource` 和证据绑定权威来源，不得看到模型输出后再挑选有利口径。
- 工具用例必须用 `expectedCallPolicy` 区分同轮立即调用、允许确认后调用和整通最终调用；脚本必须覆盖允许的确认轮次。
- `caseRole` 只表达正例、反例、边界例、问题例或保护例；业务分类和等级必须放在动态 `taxonomyValues`，不得重新引入固定 `difficulty` 或 `risk`。

### 步骤 2：分层自检与执行

先运行 `runner.py --selftest 6`，覆盖无工具、多次同工具、错误函数、Mock 耗尽、终止工具、多工具顺序等层级。自检失败必须先修装置。

正式运行必须指定 `--run-id` 与 `--run-kind`，记录提示词、工具、用例、轨迹和评分目录。`scriptedExact` 中的事实原样输出；guided 模式也必须受 `{variables}` 和用例事实约束。

### 步骤 3：评分

- `judge`、`expected` 与 `expected-semantic` 使用 `bin/scorer.py`；其中 `expected` 直接比较标准化标签或严格 JSON，不调用模型。
- `tool-binary` 使用 `bin/score_tool_binary.py`。函数名、次数、顺序、精确参数和明确时机先由程序判断；只有自由文本参数或无法程序化的语义时机才调用 Judge。
- `expected` 默认忽略文本首尾空白；JSON 对象键顺序不影响结果，但字段、数组顺序、值和类型必须一致，且默认禁止代码围栏、重复字段和 `NaN/Infinity`。特殊格式用 `deterministicScoring` 显式配置。
- `judge` 用 `prompts/judge_score.md`，`expected-semantic` 用 `prompts/expected_check.md`；关思考的 Judge 评分可加 `--judge-steps`。
- 装置错误标记 `INVALID/device_error`，不得记 0 分或进入回归集。
- 分数写入独立轮次目录并登记 manifest；`_summary.json` 不是用例。

### 步骤 3.5：失败归因与用例人工审核

- 宿主 Agent 用 `failure_triage.py` 将失败分为 `model_failure`、`case_error`、`device_error`、`scorer_error`、`spec_ambiguous`、`tool_contract_error`；只有高置信度 `model_failure` 进入问题集。
- 失败归因同时保存 `caseStatus`、动态 `taxonomyValues`、稳定性原始运行计数、任务级 `stabilityLabel` 和 `optimizationEligible`。严重度由任务分类体系解释，稳定性频率不套统一百分比。
- 归因文件必须与本轮评分目录校验并覆盖全部 `FAIL`，不得漏掉失败用例或混入非失败用例；已登记且内容未变的修正不得重复提案。
- 每条归因必须引用权威定义、完整上下文或明确观察窗口、相邻边界用例和决定性事件；使用业务字段时记录其定义、生产者和写入/推导链路，禁止按样本相关性反推语义。
- 人工争议用例使用 `case_adjudication.py` 分批记录 `keep-human | accept-model | corrected | exclude`；剔除单独写入 exclusion registry，保留完整来源和理由，不得删除原用例。
- `case_error` 只能先生成 `pending_review` 提案，不得修改正式用例。必须向用户展示原文、候选、差异、理由和影响，人工审核无误后才能使用 `case_correction_guard.py` 写入。
- 审核文件必须绑定失败归因文件哈希、提案哈希和用户确认声明；任一条无效时整批不写，写入中途失败时整批回滚。
- headless 模式遇到待审核用例必须暂停，配置模型或 Judge 不能代替人工批准。
- 已批准修正写入 `case_corrections_registry.json`。当前 `caseId + correctedSha256` 未变化时下次直接复用，不重复修正；内容变化则重新人工审核。
- 任一用例修正后旧基线失效。下一次正式执行必须向 `runner.py` 提供修正收据，以 `run-kind=full` 和完整用例集重建基线；禁止 `--limit`、子集运行或把修正前后分数直接比较。
- 专项问题集可以用 `select_cases.py --where <taxonomy筛选>` 按任意任务维度组合，但只能选择 `optimizationEligible=true` 的真实模型失败，并继续搭配相邻保护集。

### 步骤 4：回归与优化

1. 先校正用例契约；用例变化后旧分数失效，重新建立基线。
2. 按根因聚类，一轮只选一个根因；先填写 `root-cause-analysis.json` 定位最早分歧并进行单变量消融，再创建并验证 `optimization_iteration.json`。
3. 优化输入必须分别包含：通用原则、模型与任务指令、硬约束、本轮根因、需求整合分析、改动范围、问题集、相邻正确集、重复次数、任务级接受策略、能力覆盖清单和原有证据包。
4. 先运行 `optimization_journal.py start` 从父版本创建新的候选文件，再使用 `prompts/optimize.md` 对该候选做最小改动。整体重写必须有结构性理由，不能作为默认捷径。
5. 运行 `check_prompt.py --iteration-plan ... --capability-inventory ... --architecture-track ...`，验证整合位置、结构净变化和能力保留；存在硬约束时再运行 `check_constraints.py`。
6. 问题集与相邻正确集通常重复 3 次；曾出现方向不一致时通常追加到 5 次，仍不稳定则继续追加或判定证据不足，稳定后再跑全量。不得把这些重复次数当作优化轮数上限。
7. `iteration_guard.py --results` 通过后，才记录接受；否则回退本轮。无论接受、回退还是证据不足，都必须用 `optimization_journal.py complete` 追加结论、更新 `optimization-state.json` 和恢复摘要，再继续下一个可验证根因。

详细循环与命令见 [references/prompt-optimization-loop.md](references/prompt-optimization-loop.md)。

### 步骤 5：报告

- 失败候选、局部未通过和全量未通过时禁止生成正式报告，只保存 manifest、评分、回退原因和下一步摘要。
- 只有候选通过全部门禁并标记 `ACCEPTED`，或用户明确要求中间报告时，才生成报告。
- 按 manifest 或显式选择生成报告；不得扫描并混合全部分数目录。
- 最终版本来自接受/回退决策，不取编号最大的文件。
- 轨迹为 0 或卡片轮用例集合不一致时应失败，不得宣称装置正常。
- 报告必须区分运行范围、评分模式、有效用例和 INVALID；二值评分显示 `correctCount`、`wrongCount` 和 0/100 分布。
- 报告必须显示优化目标角色、提示词任务类型、目标模型画像、用户选择、硬约束数量和验证结果；不同模型或画像的结论不得混写为同口径提升。

## 关键护栏

- 优先回归经归因确认的失败用例，并同时验证相邻保护集；达到本任务 `acceptancePolicy` 的局部门禁后再全量，减少无效调用。
- 单次均分变化不能单独作为接受证据，应结合用例级修复/退化、分类或字段变化以及重复运行稳定性；是否属于噪声由本任务证据决定，不套固定百分点。
- 任何版本只有在同口径验证、无不可接受新退化并记录决策后才能成为最终版本。
- 工具描述、枚举和必填说明可以优化；函数名和参数名只有用户明确授权才可改。
- 模型画像经验是强推荐而不是厂商保证；真实同集回归证据优先。用户拒绝画像时不得暗中继续套用。
- 用户明确资源预算耗尽、所有可验证根因与未测试假设均耗尽、外部装置持续无效或用户中断时才停止。某一根因进入平台期时转向其他根因；只要仍有新证据或可测试假设就继续。
- 成功时给出最优版本路径、统计口径、提升证据、剩余失败和最终报告路径；未成功时只给当前最佳、失败摘要和下一步，不伪装成最终交付。

## 触发后的第一件事

先明确告诉用户这是“新建任务”还是“继续运行”。新建任务时展示完整配置总览；继续运行时展示 manifest 快照、当前阶段、剩余预算和变化项。确认后再按对应参考契约执行。
