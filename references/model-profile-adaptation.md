# 模型画像适配

被测模型承担工具调用，或用户要求按模型厂商、模型族和思考模式优化时读取本文件。

## 匹配原则

画像规则的消费者始终是当前宿主 Agent；`optimizationTargetRole` 指明本次要优化哪个角色的提示词。candidate、slotfiller、judge、simulator、generate 都可以成为目标，但配置模型本身均不负责修改提示词。

画像匹配使用 `provider + modelFamily + reasoning + apiStyle + capabilities`，不按 `base_url` 猜厂商。优先级：

1. 用户显式指定 `promptProfile=<画像ID>`。
2. 厂商、模型族和思考模式共同匹配。
3. 仅模型名模式匹配时降低置信度并请求确认。
4. 不明确或多个画像并列时使用 `generic`。

```bash
python bin/model_profiles.py --config config.json --task tasks/demo/task.json --role slotfiller
```

画像矩阵来自用户于 2026-09-08 确认的项目经验，不代表厂商官方保证；真实同集回归可以推翻推荐。

| 模型/模式 | 更适合的方式 | 不太适合 |
|---|---|---|
| Qwen 非思考 | 就近“条件→话术→函数”直接模仿；短规则；单状态单函数 | 跨章节状态推导、长优先级链 |
| DeepSeek 非思考 | Schema 优先＋短触发规则＋指定 `tool_choice` | 用长篇自然语言代替参数约束 |
| DeepSeek 思考 | “目标→事实→约束→调用→观察→继续”的循环 | 丢弃历史 `reasoning_content` |
| GLM 思考模式 | 显式目标、状态转换、观察结果、终止条件 | 把每个动作都写成孤立固定话术 |
| Kimi K3 | 长任务目标、少量活跃目标、完整历史、连续工具循环 | 每轮重置状态或删除推理历史 |
| Kimi Instant | 直接条件—动作规则 | 要求模型隐式规划复杂链路 |
| MiniMax M 系列 | 完整工具契约；使用/禁用/返回/失败分别写清 | “需要时调用”等模糊描述 |
| MiniMax M3 Adaptive | 硬规则＋高层目标的双层结构 | 所有场景都强制思考或都禁止思考 |
| 豆包 Function Call | 意图—函数映射、少工具、强 Schema、确定节点强制调用 | 在语音未结束时提前提交写操作 |

## 五层落位

| layer | 应做什么 | 不应做什么 |
|---|---|---|
| `prompt` | 调整规则结构、触发表达、目标和终止条件 | 修改业务事实或接口 |
| `api` | 思考控制、API 风格、请求参数 | 写成业务提示词补丁 |
| `context` | 保存工具结果、状态和必要推理历史 | 依赖模型凭记忆恢复 |
| `tool` | 动态暴露工具、Schema、`tool_choice` | 让长提示词代替硬约束 |
| `guard` | 状态、权限、幂等、语音终点和参数校验 | 把非法动作交给模型自觉避免 |

只有 `hostOptimizationDirectives` 进入宿主 Agent 的优化输入。`runtimeRequirements` 只进入预检和 `runtimeFindings`，不能直接拼进目标提示词。

## 任务类型过滤

`promptTaskType` 显式值优先；否则 slotfiller、judge、simulator、generate 分别推断为 `slot-filling`、`judge`、`simulator`、`case-generation`，candidate 再按 tools 与 taskType 推断。

宿主 Agent 按三层合成优化指令：

1. 模型通用指令：例如 Qwen 非思考偏短规则和就近条件动作映射。
2. 任务类型指令：分类、提槽、固定 JSON、抽取、总结、RAG、生成、Judge、模拟用户、普通对话或工具 Agent 各有独立契约。
3. 模型×任务专用指令：例如豆包提槽强调槽位来源、强 Schema、缺参结果和纯 JSON；不会附加 Function Call 或语音写操作门禁。

`runtimeRequirements` 也必须按目标角色和任务类型过滤。没有作用域的历史运行要求默认继承画像 `taskTypes`，不得向无关任务泄漏。

## 用户确认

模型画像确认不能替代通用优化原则确认。宿主 Agent 必须先逐项展示全局审视、不乱打补丁、提示词精简、结构证据优先、字段来源可追溯、时间锚点与观察窗口、正反边界成对验证、变量占位符不可动、不删除既有功能、失败根因优先、保持原格式、函数名参数名不可动，再单独展示画像推荐。

命中专用画像时，宿主 Agent 向用户展示：模型与思考模式、画像 ID、匹配依据和置信度、强推荐原则、运行时要求、不推荐结构。让用户选择：

- 全部接受：选中原则升级为 `required`。
- 修改后接受：保留用户修改文本，禁止画像默认值覆盖。
- 不采用：记录拒绝，不生成硬约束。

创建快照示例：

```bash
python bin/profile_constraints.py build --profile qwen-nonthinking \
  --status accepted --accept QWEN-NONTHINK-01 --accept QWEN-NONTHINK-02 \
  --principle PRINCIPLE-GLOBAL-REVIEW --principle PRINCIPLE-ROOT-CAUSE-FIRST \
  --principle PRINCIPLE-PRESERVE-PLACEHOLDERS --confirm-principles \
  --target-role candidate --task-type tool-agent --preamble-owner host \
  --out runs/demo/optimization_constraints.json
```

工具型语音任务必须确认垫词归属：`model` 表示模型同轮输出垫词并调用；`host` 表示模型只调用，宿主播放垫词。

## 验证门禁

优化器先输出每条硬约束的 `constraintCompliance`，再执行：

```bash
python bin/check_constraints.py --config config.json \
  --constraints runs/demo/optimization_constraints.json \
  --prompt runs/demo/prompt_v2.md \
  --compliance runs/demo/constraint_compliance.json \
  --out runs/demo/constraint_validation.json
```

静态门禁检查约束 ID 和证据完整性；Judge 独立核对候选提示词。`FAIL`、`UNVERIFIABLE`、解析失败或 Judge 不可用都退出 1。只有 PASS 候选才能传给 runner 的 `--constraints` 与 `--constraint-validation` 进入正式回归。

Runner 会核对回执结构、硬约束逐项 PASS 证据、画像 ID、提示词哈希和约束快照哈希，防止遗漏字段或串用旧回执。这属于本地完整性门禁，不是密码学身份认证：能够以同一系统用户修改脚本、回执和运行目录的进程也能伪造这些产物。存在不可信写入方时，必须用操作系统权限隔离运行目录，或由外部可信服务签名；不得宣称本地 JSON 能证明 Judge 一定被调用。

schemaVersion 1/2/3 的旧约束快照一律必须迁移到 schemaVersion 4，并重新确认包含结构证据、字段溯源、时间锚点和正反边界在内的完整通用优化原则后才能继续；即使旧快照为 rejected 或硬约束为空，也不得用默认值猜测用户选择。

## 比较口径

先在同一 candidate 上比较通用基线与画像适配版，保持用例、工具、温度和运行装置一致。不同 candidate、不同画像或不同主观 Judge 产生的分数必须分开呈现，不能合并成一条提升曲线。
