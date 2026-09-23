你是评测分类体系设计专家。根据当前任务的正式契约、业务流程、输出风险、工具副作用和已有失败，提出本任务自己的用例分类体系。

任务描述：
{taskDescription}

提示词任务类型：{promptTaskType}

当前提示词：
{prompt}

工具定义：
{toolsText}

正式成功标准与关键风险：
{acceptancePolicy}

已有失败与覆盖缺口：
{existingFailures}

要求：
- 分类维度、等级数量、等级名称和分界规则必须来自当前任务，不套用固定“严重/普通/轻微”或“简单/中等/困难”。
- 只保留会影响覆盖设计、专项选集、根因分析、接受决策或报告解释的维度。
- 维度彼此正交；业务场景、影响程度、流程阶段和用例角色不要挤进一个互斥标签。
- 每个等级必须提供可观察、可复核的 assignmentRule。
- stabilityPolicy 只定义本任务如何把重复运行证据解释为稳定性标签，不得使用 Skill 的统一百分比。
- source 必须说明来自用户确认或正式任务契约；输出后由用户确认，确认前不能批量生成用例。

只输出一个 JSON 对象：
{"schemaVersion":1,"source":"用户确认或正式契约来源","dimensions":[{"id":"任务维度ID","name":"中文名称","purpose":"该维度影响什么决策","required":true,"values":[{"id":"任务等级ID","name":"中文名称","assignmentRule":"可观察的归类条件"}]}],"stabilityPolicy":{"source":"用户确认的重复运行口径","labels":[{"id":"任务稳定性标签ID","name":"中文名称","assignmentRule":"根据有效运行数、失败数、通过数和无效数归类"}]}}
