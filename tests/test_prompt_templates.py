# -*- coding: utf-8 -*-
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import gen_cases


class PromptTemplateContractTest(unittest.TestCase):
    def read(self, relative):
        with open(os.path.join(ROOT, relative), encoding="utf-8") as f:
            return f.read()

    def test_simulator_receives_locked_case_facts(self):
        text = self.read("prompts/simulate_user.md")
        self.assertIn("{variables}", text)
        self.assertIn("用例既定事实", text)
        self.assertIn("不得自行编造", text)
        self.assertIn("冲突时以变量快照为准", text)

    def test_judges_share_generic_fact_and_tool_contract_boundaries(self):
        for name in ("judge_score.md", "judge_score_step.md"):
            text = self.read("prompts/" + name)
            self.assertIn("运行时事实快照", text)
            self.assertIn("toolsExpect 为空", text)
            self.assertIn("期望工具为无时", text)
            self.assertIn("unexpected_tool_call", text)
            self.assertIn("INVALID", text)
            self.assertIn("语义对应", text)
            self.assertNotIn("优先归入 `tool_use`", text)
            for business_word in ("门店", "订餐", "排号"):
                self.assertNotIn(business_word, text)

    def test_step_judge_output_instructions_do_not_contradict_each_other(self):
        text = self.read("prompts/judge_score_step.md")
        self.assertIn("先输出 5 个编号步骤", text)
        self.assertIn("JSON 之后不得再输出文字", text)
        self.assertNotIn("最后只输出一个 JSON 对象（不要任何其他文字）", text)

    def test_case_generation_contains_v1_2_contracts_and_generic_examples(self):
        text = self.read("prompts/gen_cases.md")
        for term in ("terminalTools", "scriptedExact", "derivedToolArgs",
                     "emptyEquivalentToolArgs", "参数组数量", "顺序 Mock 数量"):
            self.assertIn(term, text)
        self.assertNotIn("顾客先报人数", text)
        self.assertIn("{caseTaxonomy}", text)
        self.assertIn("taxonomyValues", text)
        self.assertNotIn('"difficulty"', text)

    def test_case_generation_renders_new_task_contract_placeholders(self):
        task = {
            "description": "demo", "initialPrompt": "system", "tools": [],
            "promptTaskType": "classification", "evalMode": "expected",
            "terminalTools": ["handoff"], "derivedToolArgs": ["request.id"],
            "emptyEquivalentToolArgs": ["note"],
        }
        rendered = gen_cases.build_prompt(
            task, {"category": "normal", "focus": "flow"}, 1, 1)
        self.assertIn("handoff", rendered)
        self.assertIn("request.id", rendered)
        self.assertIn("note", rendered)
        self.assertIn("classification", rendered)
        self.assertIn("程序确定性", rendered)
        for placeholder in ("{terminalTools}", "{derivedToolArgs}", "{emptyEquivalentToolArgs}"):
            self.assertNotIn(placeholder, rendered)

    def test_optimizer_accepts_full_context_and_two_output_strategies(self):
        text = self.read("prompts/optimize.md")
        for placeholder in ("{scoreMode}", "{runKind}", "{manifestSummary}",
                            "{tools}", "{toolChanges}", "{caseSetScope}",
                            "{acceptedVersion}", "{rollbackHistory}",
                            "{optimizationStrategy}"):
            self.assertIn(placeholder, text)
        self.assertIn("整体重写", text)
        self.assertIn("局部编辑", text)
        self.assertIn("修改块", text)
        self.assertNotIn("整体输出，不要用 diff 描述", text)

    def test_optimizer_receives_model_profile_and_emits_constraint_evidence(self):
        text = self.read("prompts/optimize.md")
        for placeholder in ("{modelProfile}", "{hardConstraints}",
                            "{runtimeRequirements}", "{constraintValidation}",
                            "{optimizationTargetRole}", "{promptTaskType}",
                            "{universalOptimizationPrinciples}", "{modelOptimizationDirectives}"):
            self.assertIn(placeholder, text)
        self.assertIn("constraintCompliance", text)
        self.assertIn("runtimeFindings", text)
        self.assertIn("当前宿主 Agent", text)
        self.assertIn("配置模型不负责优化", text)
        for placeholder in ("{rootCauseHypothesis}", "{changeScope}",
                            "{problemCaseIds}", "{neighborPassCaseIds}",
                            "{repeatRuns}", "{acceptancePolicy}",
                            "{requirementIntegration}"):
            self.assertIn(placeholder, text)
        for term in ("先查找已有权威规则", "merge-existing", "generalize-replace",
                     "new-local-rule", "rulesReplacedOrRemoved", "integrationCompliance"):
            self.assertIn(term, text)
        for placeholder in ("{authoritativeDefinitions}", "{fieldProvenance}",
                            "{decisionArchitecture}", "{boundaryPairs}",
                            "{observationWindow}"):
            self.assertIn(placeholder, text)

    def test_task_analysis_covers_scoring_and_tool_risks(self):
        text = self.read("prompts/task_analysis.md")
        for placeholder in ("{evalMode}", "{toolsText}", "{terminalTools}", "{existingFailures}",
                            "{optimizationTargetRole}", "{promptTaskType}"):
            self.assertIn(placeholder, text)
        self.assertIn("错误函数", text)
        self.assertIn("多次调用", text)
        for term in ("结构证据", "字段来源", "决定性事件", "正反边界"):
            self.assertIn(term, text)

    def test_expected_check_uses_match_level_not_partial_verdict(self):
        text = self.read("prompts/expected_check.md")
        self.assertIn('"matchLevel":"exact|partial|wrong"', text)
        self.assertNotIn('"verdict":"PASS|PARTIAL|FAIL"', text)

    def test_slot_fill_reports_missing_required_without_guessing(self):
        text = self.read("prompts/slot_fill.md")
        self.assertIn("JSON Schema", text)
        self.assertIn("枚举", text)
        self.assertIn('"missingRequired"', text)

    def test_binary_prompt_is_external_and_loaded_by_script(self):
        prompt_path = os.path.join(ROOT, "prompts", "tool_binary_score.md")
        self.assertTrue(os.path.isfile(prompt_path))
        import score_tool_binary
        text = score_tool_binary.load_binary_prompt()
        self.assertIn("{expectedTools}", text)
        self.assertIn("{actualTools}", text)
        self.assertIn("不得按固定第几轮", text)
        self.assertIn("合理追问不算晚调用", text)
        source = self.read("bin/score_tool_binary.py")
        self.assertNotIn('PROMPT = """', source)

    def test_markdown_report_template_uses_manifest_scope_and_binary_fields(self):
        text = self.read("templates/report.md")
        for term in ("run_manifest.json", "运行范围", "评分模式", "有效用例",
                     "INVALID", "correctCount", "最终版本决策", "模型画像",
                     "硬约束验证", "根因假设", "相邻正确集", "重复验证"):
            self.assertIn(term, text)

    def test_skill_routes_details_to_references_and_version_is_1_4_11(self):
        skill = self.read("SKILL.md")
        self.assertLessEqual(len(skill.splitlines()), 350)
        self.assertIn("references/execution-contract.md", skill)
        self.assertIn("references/prompt-optimization-loop.md", skill)
        self.assertIn("references/model-profile-adaptation.md", skill)
        self.assertEqual("1.4.11", self.read("VERSION").strip())

    def test_taxonomy_prompt_derives_dimensions_from_current_task(self):
        text = self.read("prompts/case_taxonomy.md")
        for placeholder in ("{taskDescription}", "{promptTaskType}", "{prompt}",
                            "{toolsText}", "{acceptancePolicy}", "{existingFailures}"):
            self.assertIn(placeholder, text)
        self.assertIn("不套用固定", text)
        self.assertIn("assignmentRule", text)


if __name__ == "__main__":
    unittest.main()
