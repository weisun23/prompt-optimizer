# 执行装置契约

本文件定义工作区、任务、用例、模拟用户、工具调用、Mock、自检和运行清单的确定性规则。进入任务分析、用例生成或执行阶段前读取。

## 工作区布局

```text
workspace/
├── config.json
├── .skill-source.json
├── bin/
├── prompts/
├── profiles/model-profiles.json
├── reports/<task-name>/<run-name>/
└── tasks/<task-name>/
    ├── task.json
    ├── case-taxonomy.json
    ├── cases/generated/
    ├── cases/custom/
    ├── cases/regress.json
    └── runs/<run-name>/
        ├── prompt_v1.md ...
        ├── tools_v1.json ...
        ├── rounds.json
        ├── optimization-state.json
        ├── OPTIMIZATION_SUMMARY.md
        ├── run_manifest.json
        ├── trajectories-*/
        └── scores/*/
```

初始化只在目标不存在时复制 Skill 的 `bin/*.py`、`prompts/*.md` 和配置示例。已存在的项目副本属于项目，不覆盖。

`.skill-source.json` 至少记录：

```json
{"skill":"prompt-optimizer","version":"1.3.0","copiedAt":"YYYY-MM-DD"}
```

## 模型与配置

角色：

- `generate`：任务分析与用例生成。
- `simulator`：生成对话型下一条用户消息。
- `candidate`：被测模型。
- `slotfiller`：text-protocol 下抽取函数参数；未配置时可复用 candidate，但必须在总览中明确。
- `judge`：仅 `judge`、`expected-semantic`、含 `semanticToolArgFields` 的 `tool-binary` 或语义硬约束验证使用。纯精确工具契约和确定性 `expected` 不需要该角色。

配置由用户提供。不得写死示例模型，不得探测其他 API。API key 优先引用环境变量，不在报告或日志中输出。

每个角色可声明 `provider`、`modelFamily`、`apiStyle`、`promptProfile`、`toolChoicePolicy` 和 `capabilities`。除 `slotfiller` 可在用户确认后复用 candidate 外，任何缺失角色都不得静默复用其他模型。画像匹配和确认见 [model-profile-adaptation.md](model-profile-adaptation.md)。

`toolChoicePolicy` 支持 `auto`、`none`、`required` 和 `named:<函数名>`；指定不存在的函数或无 tools 时要求 required 必须失败。DeepSeek 思考模式或 `capabilities.reasoningReplay=true` 时，assistant 的 `reasoning_content` 必须随历史回传。

### 评分模式选择

先执行 `python bin/evaluation_mode.py --task <task.json>`，以输出的 `evalMode`、`comparisonMethod` 和 `scoringJudgeRequired` 生成配置总览；不得仅凭是否存在 `models.judge` 决定是否向用户展示评分模型。

- `expected`：纯程序确定性评分。分类、提槽、固定 JSON、结构化抽取在 `auto` 下使用；不要求 `models.judge`，每条只比较一次，Judge 调用 0 次。
- `expected-semantic`：期望输出固定但允许同义表达、信息重排或其他无法可靠程序化的语义等价时使用 Judge。
- `judge`：多维度主观质量评分。
- `tool-binary`：工具调用专项；名称、次数、顺序、明确时机和精确参数由程序判断。只有任务显式声明 `semanticToolArgFields` 时才配置 Judge 判定对应自由文本语义。

`expected` 的文本默认只去除首尾空白，大小写敏感；JSON 默认必须是无代码围栏的单一合法 JSON 值，对象键顺序可不同，但字段集合、数组顺序、值和类型必须一致。输出缺少 `expectedOutput` 时标记 `INVALID/case_error`，不得调用 Judge 补判。若这些规则不符合业务，先在 task 中显式调整 `deterministicScoring`，不能静默改用评分模型。

## task.json 关键字段

- `description`、`initialPrompt`、`taskType`、`optimizationTargetRole`、`promptTaskType`、`evalMode`
- `expectedOutputFormat`：确定性输出格式，`auto | text | json`
- `deterministicScoring`：程序评分选项；支持 `format`、`trimText`、`caseSensitive`、`allowJsonFence`、`metricPaths`
- `evaluationSourcePrecedence`：当输入、业务字段、嵌入标签或人工标签可能冲突时，按高到低列出决定 `expectedOutput` 的权威来源
- `judgePassThreshold`、`scoreDisagreementThreshold`：仅使用 Judge 时必填，取值必须来自本任务正式口径或用户确认，不提供全局默认值
- `userTemplate`：inject 输入模板；`userInputTemplate`：chat 用户消息句式
- `userFormat`：chat 消息使用占位符、纯文本或混合格式
- `variables`、`dimensions`、`constraints`
- `literalPlaceholders`：只作为字面示例保留、不应被渲染的 ASCII 大括号名称
- `tools`、`toolMode`、`toolMockPolicy`
- `systemToolArgs`：由系统或运行环境提供、不要求用户脚本显式给出的参数路径
- `terminalTools`：调用后终止对话的函数名
- `derivedToolArgs`：允许由工具返回或上下文派生的参数路径
- `emptyEquivalentToolArgs`：空值与缺省可视作等价的参数路径
- `simulateMode`、`maxTurns`
- `roleToneWords`：用例角色口吻检测词；空数组表示关闭该告警
- `coveragePlan`、`acceptancePolicy`、`capabilityInventory`、`baselineSelection`：分别指向覆盖、接受、能力保留与基线选择契约
- `caseTaxonomy`：用户确认的任务级分类体系路径和 SHA-256；分类维度、等级与稳定性解释不得写死在 Skill 中
- `reportHtml`

优化总轮数、用例总数、每类数量、修复率和允许退化量均无通用默认值。用例数量由覆盖项决定；接受标准由任务契约或用户确认；调用、费用或时间上限只有在用户明确提供时才写入资源预算。

提示词中的业务占位符属于被测系统，生成用例时必须保持原样。控制模板占位符由脚本安全替换，不用通用 `str.format()` 解释业务大括号。

## 任务分析与用例生成

### 任务分析

先填充 `prompts/case_taxonomy.md`，根据当前任务的业务契约、流程、工具副作用、输出风险和既有失败提出分类体系。向用户展示每个维度、等级和 `assignmentRule`，确认后运行 `case_taxonomy.py` 校验并冻结哈希。只保留会影响覆盖、专项选集、根因分析、接受决策或报告解释的维度。

填充 `prompts/task_analysis.md`：

- `{taskDescription}`、`{prompt}`、`{taskType}`、`{evalMode}`
- `{toolsText}`、`{terminalTools}`、`{existingFailures}`

输出场景分类、风险点、覆盖项映射、每类按覆盖需要确定的数量和说明。不得先设固定总量再截断风险项。工具任务至少覆盖：正常调用、无需调用、错误函数、多次调用、顺序、参数边界、终止工具和异常恢复。

### 生成命令

```bash
python <workspace>/bin/gen_cases.py --config <config.json> --task <task.json> \
  --plan <plan.json> --taxonomy <case-taxonomy.json> \
  --coverage-plan <coverage-plan.json> \
  --out <cases/generated> --pilot 1

python <workspace>/bin/gen_cases.py --config <config.json> --task <task.json> \
  --plan <plan.json> --taxonomy <case-taxonomy.json> \
  --coverage-plan <coverage-plan.json> \
  --out <cases/generated> --full
```

计划中每个类别必须显式填写 `coverageItemIds`、`count` 和需要试点时的 `pilotCount`；`gen_cases.py` 会拒绝未知覆盖 ID 或遗漏任何 pending 覆盖项。示例命令中的 `--pilot 1` 只是用户本次明确选择的值，不是 Skill 默认上限。先生成足以检查契约的样例并审核，再完成所有尚未覆盖项。生成器可按用户预算拆批，失败时缩小单批请求重试；不得静默降低覆盖范围或修改场景比例。

### 用例契约

- 每个 ID 唯一；类别和场景可追溯到计划。
- `caseRole` 只描述正例、反例、边界例、问题例或保护例；`taxonomyValues` 必须满足任务级分类体系。禁止使用 Skill 固定的 `critical|normal` 或 `easy|medium|hard` 代替任务分类。
- `variables` 覆盖系统和用户模板需要的变量；`literalPlaceholders` 中声明的字面示例不参与校验。
- chat 用例必须在 `chat.messages` 中提供至少一条 user 消息；guided 模式可附 `flowGuide`，scripted 模式按这些 user 消息逐轮提供台词。
- `scriptedExact=true` 是事实锁定开关，运行时原样发送 `chat.messages` 中的 user 台词，不允许 simulator 改写人数、时间、名称、标识等事实。
- 所有期望结果与工具前置条件必须列入 `modelVisibleEvidence`，并标明 `system | user-message | variable | tool-result` 来源；声明值必须真实出现在相应模型输入。`scenario` 与未实际发送的 `flowGuide` 只能帮助生成和审查，不能当作模型可见证据。
- 配置 `evaluationSourcePrecedence` 后，含 `expectedOutput` 的用例必须填写 `expectedOutputSource` 与 `expectedOutputSourceEvidence`；来源冲突时按任务优先级选择，不得看到模型输出后反向挑选有利标签。
- `toolsExpect` 每出现一次函数，就表示期望一次调用；次数和顺序都具有语义。
- 工具用例必须填写 `expectedCallPolicy`：`immediate` 表示条件满足后的首个助手轮必须调用，`after-confirmation` 明确允许的澄清次数，`eventual` 明确最迟附加轮次。不得把“本轮立即”和“整通最终”混成一个正确性口径。
- `expectedToolArgs[function]` 参数组数量必须等于该函数在 `toolsExpect` 中出现的次数。
- 多函数顺序必须可由脚本或 flowGuide 推进。
- 每个期望调用的顺序 Mock 数量必须充足；终止工具之后不得再安排调用。
- `derivedToolArgs` 和 `emptyEquivalentToolArgs` 只放明确允许的路径。
- 用例脚本必须能提供所有不可由 `systemToolArgs` 或 `derivedToolArgs` 派生的期望参数，禁止评分器靠猜测补齐。
- `userFormat` 决定生成消息是否保留占位符；`roleToneWords` 只做角色口吻告警，不改变用例内容。

## 模拟用户契约

### scriptedExact

逐条原样输出。不得调用 simulator，不做同义改写。脚本耗尽但任务未终止时标记 `device_error`。

### scripted

按顺序提供意图，可允许有限自然表达；关键事实仍以 `variables` 和用例为准。若业务要求逐字稳定，改用 scriptedExact。

### guided

填充 `prompts/simulate_user.md` 的 `{scenario}`、`{flowGuide}`、`{variables}`、`{mode}`，并附当前完整对话。simulator 只能扮演用户、每次输出一条自然消息，不得编造、修改用例事实或替系统回答。

## 工具调用契约

### native

把 task 中工具 schema 作为模型原生 tools。记录函数名、参数、返回和时机。

### text-protocol

candidate 按提示词输出函数名协议；slotfiller 使用 `prompts/slot_fill.md` 和 JSON Schema 从对话抽参；执行器用 Mock 返回结果。提槽缺少 required 参数时报告 `missingRequired`，不得猜测。

### Mock 语义

- 期望路径：Mock 必须存在且容量充足；缺失或耗尽为 `device_error`。
- 模型错误调用一个 schema 中已知但用例未期望的函数：记录 `unexpected_tool_call`，使用安全的合成错误结果继续或结束；这是被测模型缺陷，不是装置错误，不要求为每个错误分支预置 Mock。
- 调用未知函数、参数解析失败或违反协议：记录工具误用证据；只有装置自身无法生成可判定轨迹时才 INVALID。
- terminalTools 调用后进入终态，防止多余追问或重复调用。

## 分层自检

正式批量执行前运行：

```bash
python <workspace>/bin/runner.py --config <config.json> --task <task.json> \
  --prompt <prompt.md> --cases <cases> --out <selftest-trajectories> --selftest 6
```

六层至少覆盖：

1. 无工具正常对话或注入。
2. 单工具成功调用与参数。
3. 同一函数多次调用及顺序 Mock。
4. 错误函数或意外调用分支。
5. Mock 缺失/耗尽被识别为 INVALID。
6. terminalTools、多工具顺序或状态变更后重新处理等复杂终态。

固定取前三条不能替代分层自检。自检失败先修用例或装置，禁止通过补业务提示词掩盖。

`--selftest 6` 表示这次要抽取六个不同风险层级，不代表永远只测六条；若覆盖计划包含更多关键装置风险，必须扩大自检范围或按风险分组执行，直到所有关键层均有证据。

## 优化前置门禁

- `coverage-plan.json` 绑定 `case-taxonomy.json` 哈希，并列出每个契约、状态转换和边界的动态分类取值及正反用例；所有任务定义的关键项完成后才可称用例充分。
- `optimization_iteration.json` 必须使用最新版 schema，并通过 `requirementIntegration` 记录新增需求检查过的现有规则、合并策略、目标位置、被替换项和结构净影响；未完成整合分析不得创建候选版本。
- `capability-inventory.json` 保存生产版本已有能力；未获用户授权不得因精简候选而删除或降级。候选进入回归前用 `check_prompt.py --capability-inventory ... --architecture-track ...` 校验。
- `baseline-selection.json` 只能在相同 candidate 配置哈希、相同用例集哈希和相同生产模式中选择基线，并显式记录选择理由；不得使用编号最大或文件最新的版本。
- `acceptance-policy.json` 记录本任务成功条件、关键用例与保护用例。确定性任务逐条判定；随机任务使用用户确认的目标与稳定性规则。不得套用通用 80% 修复率、60 分及格线或固定退化数。
- `optimization-state.json` 记录仍可验证的根因、假设和新证据。候选失败时回退并继续；只有成功、用户停止、明确资源预算耗尽或确无可验证路径时才终止。

## 正式执行与 manifest

```bash
python <workspace>/bin/runner.py --config <config.json> --task <task.json> \
  --prompt <prompt.md> --cases <cases> --out <trajectories-dir> \
  --run-id <unique-id> --run-kind <full|regression|stability|candidate> \
  --constraints <optimization_constraints.json> \
  --constraint-validation <constraint_validation.json> \
  [--case-correction-receipt <case-correction-receipt.json>]
```

存在 `.rebaseline-required.json` 待处理标记时，`--case-correction-receipt` 必填，且只能执行 `run-kind=full` 的完整用例集；不得同时使用 `--limit`。收据、标记、当前用例集哈希、用例 ID 清单或数量任一不一致都会在模型调用前失败。

执行前可使用 `--estimate` 预估调用量。每次非自检运行都创建或追加 `run_manifest.json` invocation，不覆盖历史。invocation 至少记录：

- run id/kind 与时间
- 提示词、工具、用例快照和 SHA-256
- 选中用例 ID、数量和选集哈希
- 模型角色与关键参数快照
- candidate 模型画像、匹配置信度、工具选择策略、用户约束及验证哈希
- 用例修正收据、用例集清单与重建基线完成状态（若适用）
- 轨迹目录、评分目录、评分模式和评分器版本

中断后从已落盘产物继续；不得复用不同提示词或用例的轨迹冒充同一轮。
