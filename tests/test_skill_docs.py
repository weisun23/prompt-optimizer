# -*- coding: utf-8 -*-
import json
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SkillDocsTest(unittest.TestCase):
    def read(self, relative):
        with open(os.path.join(ROOT, relative), encoding="utf-8") as f:
            return f.read()

    def test_version_is_1_4_11(self):
        self.assertEqual("1.4.11", self.read("VERSION").strip())

    def test_skill_distinguishes_new_and_resume_workflows(self):
        text = self.read("SKILL.md")
        self.assertIn("新建任务", text)
        self.assertIn("继续运行", text)
        self.assertNotIn("初始化（每次必做）", text)
        self.assertIn("只确认发生变化", text)

    def test_skill_documents_native_binary_and_manifest_contracts(self):
        text = self.read("SKILL.md")
        for term in ("tool-binary", "score_tool_binary.py", "run_manifest.json",
                     "terminalTools", "scriptedExact", "--selftest 6"):
            self.assertIn(term, text)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "references", "scoring-and-manifest.md")))

    def test_readme_uses_cc_switch_and_both_hosts(self):
        text = self.read("README.md")
        self.assertIn(".cc-switch/skills/prompt-optimizer", text)
        self.assertIn("Claude Code", text)
        self.assertIn("Codex", text)
        self.assertIn("$prompt-optimizer", text)

    def test_readme_explains_product_workflow_differentiation_and_limits(self):
        text = self.read("README.md")
        for heading in ("## 它解决什么问题", "## 适用场景", "## 工作原理",
                        "## 与常见方案的设计差异", "## 快速开始", "## 产物与可复现性",
                        "## 使用边界"):
            self.assertIn(heading, text)
        for term in ("宿主 Agent 是优化执行者", "失败归因", "相邻保护集", "任务级接受策略",
                     "程序确定性评分", "人工审核", "run_manifest.json"):
            self.assertIn(term, text)
        self.assertIn("不是针对具体竞品的跑分结论", text)
        for unsupported_claim in ("业界第一", "最强", "全面领先", "保证提升"):
            self.assertNotIn(unsupported_claim, text)

    def test_templates_expose_new_contract_fields(self):
        score = json.loads(self.read("templates/score.json"))
        case = json.loads(self.read("templates/case.json"))
        self.assertIn("scoreMode", score)
        self.assertIn("totalScore", score)
        self.assertIn("scriptedExact", case["chat"])
        self.assertEqual(len(case["toolsExpect"]), len(case["expectedToolArgs"]["example_tool"]))
        self.assertIn("modelVisibleEvidence", case)
        self.assertIn("expectedCallPolicy", case)
        self.assertIn("taxonomyValues", case)
        self.assertIn("caseRole", case)
        self.assertNotIn("difficulty", case)

    def test_reference_commands_match_cli_and_document_active_fields(self):
        execution = self.read("references/execution-contract.md")
        loop = self.read("references/prompt-optimization-loop.md")
        self.assertIn("--pilot 1", execution)
        self.assertIn("--full", execution)
        self.assertNotIn("--decision <accepted|rejected|rolled_back>", loop)
        for field in ("literalPlaceholders", "systemToolArgs", "userFormat", "roleToneWords"):
            self.assertIn(field, execution)
        self.assertNotIn("`firstUser`", execution)
        self.assertIn("`chat.messages`", execution)
        for term in ("deterministicScoring", "expectedOutputFormat", "expected-semantic"):
            self.assertIn(term, execution)
        triage = self.read("references/failure-triage-and-case-review.md")
        self.assertIn("--scores", triage)
        self.assertIn("--case-correction-receipt", triage)

    def test_skill_preserves_partial_stage_workflow(self):
        text = self.read("SKILL.md")
        self.assertIn("阶段范围", text)
        self.assertIn("只评分", text)

    def test_skill_requires_profile_confirmation_and_constraint_gate(self):
        text = self.read("SKILL.md")
        for term in ("model-profile-adaptation.md", "强烈推荐", "用户确认",
                     "optimization_constraints.json", "check_constraints.py"):
            self.assertIn(term, text)
        self.assertTrue(os.path.isfile(os.path.join(
            ROOT, "references", "model-profile-adaptation.md")))
        self.assertIn("宿主 Agent 是唯一优化执行者", text)
        self.assertIn("配置模型不负责修改提示词", text)
        self.assertIn("optimizationTargetRole", text)
        self.assertIn("全部提示词类型", text)
        for term in ("全局审视", "提示词精简", "失败根因优先", "保持原格式"):
            self.assertIn(term, text)
        for term in ("单根因", "相邻正确集", "重复验证", "全量回归", "iteration_guard.py"):
            self.assertIn(term, text)
        for term in ("人工审核", "pending_review", "failure_triage.py",
                     "case_correction_guard.py", "case_corrections_registry.json"):
            self.assertIn(term, text)
        review = json.loads(self.read("templates/case-correction-review.json"))
        self.assertEqual(2, review["schemaVersion"])
        self.assertIn("triageSha256", review)
        self.assertIn("approvalStatement", review)
        for term in ("expected-semantic", "Judge 调用 0 次", "不展示 judge"):
            self.assertIn(term, text)
        score = json.loads(self.read("templates/score.json"))
        self.assertIn("comparisonMethod", score)
        self.assertIn("judgeCalled", score)
        iteration = json.loads(self.read("templates/optimization-iteration.json"))
        self.assertEqual(3, iteration["schemaVersion"])
        self.assertEqual(3, iteration["repeatRuns"])
        self.assertEqual(5, iteration["unstableRepeatRuns"])
        self.assertTrue(iteration["boundaryPairs"])
        integration = iteration["requirementIntegration"]
        for field in ("newRequirement", "searchedLocations", "existingRuleLocations",
                      "sharedDecisionAxis", "strategy", "targetLocations",
                      "rulesReplacedOrRemoved", "newRuleJustification",
                      "expectedStructureEffect"):
            self.assertIn(field, integration)
        triage = json.loads(self.read("templates/failure-triage.json"))
        self.assertEqual(3, triage["schemaVersion"])
        for field in ("authoritativeBasis", "neighborCaseIds", "decisionAnchor",
                      "fieldsUsed", "fieldProvenance", "caseStatus", "taxonomyValues",
                      "stabilityEvidence", "stabilityLabel", "optimizationEligible"):
            self.assertIn(field, triage["items"][0])
        for term in ("结构证据优先", "字段来源可追溯", "时间锚点", "正反边界"):
            self.assertIn(term, text)
        for template in ("coverage-plan.json", "acceptance-policy.json", "capability-inventory.json",
                         "case-adjudication.json", "root-cause-analysis.json", "optimization-state.json",
                         "baseline-selection.json", "case-taxonomy.json"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "templates", template)))
        coverage = json.loads(self.read("templates/coverage-plan.json"))
        self.assertEqual(2, coverage["schemaVersion"])
        self.assertIn("taxonomy", coverage)
        self.assertNotIn("risk", coverage["coverageItems"][0])
        for term in ("持续优化", "失败候选不生成正式报告", "能力覆盖清单", "模型可见证据"):
            self.assertIn(term, text)
        for term in ("需求整合分析", "优先合并", "无法合并", "禁止新增顶部补丁块"):
            self.assertIn(term, text)

    def test_skill_requires_versioned_attempts_and_persistent_resume_summary(self):
        text = self.read("SKILL.md")
        for term in ("optimization_journal.py", "OPTIMIZATION_SUMMARY.md",
                     "每次实际修改提示词", "禁止覆盖已有版本", "产生新结论"):
            self.assertIn(term, text)
        state = json.loads(self.read("templates/optimization-state.json"))
        self.assertEqual(2, state["schemaVersion"])
        for field in ("currentBestPrompt", "currentCandidate", "activeAttemptId",
                      "lastCompletedAttemptId", "verifiedConclusions",
                      "rejectedDirections", "nextAction", "updatedAt"):
            self.assertIn(field, state)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "bin", "optimization_journal.py")))

    def test_example_config_is_provider_neutral_and_profile_aware(self):
        config = json.loads(self.read("config.example.json"))
        candidate = config["models"]["candidate"]
        for field in ("provider", "modelFamily", "apiStyle", "promptProfile",
                      "toolChoicePolicy", "capabilities"):
            self.assertIn(field, candidate)
        self.assertNotIn("api.deepseek.com", self.read("config.example.json"))

    def test_generic_templates_do_not_contain_business_examples_or_typo(self):
        paths = [
            "prompts/slot_fill.md",
            "references/execution-contract.md",
            "templates/case.json",
            "templates/trajectory.json",
        ]
        text = "\n".join(self.read(path) for path in paths)
        for term in ("缧失", "取消重排", "本通话的标签", "包间", "订餐", "排号"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
