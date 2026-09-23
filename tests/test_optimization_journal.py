# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import optimization_journal


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def valid_plan():
    return {
        "schemaVersion": 3,
        "workflowId": "evidence-driven-single-hypothesis",
        "rootCauseHypothesis": "首次状态转换边界不清",
        "changeMode": "local",
        "architectureTrack": "production-candidate",
        "changeScope": ["状态转换规则"],
        "requirementIntegration": {
            "newRequirement": "修正状态转换边界",
            "searchedLocations": ["状态转换规则", "异常处理规则"],
            "existingRuleLocations": ["状态转换规则"],
            "sharedDecisionAxis": "是否满足状态转换条件",
            "strategy": "merge-existing",
            "targetLocations": ["状态转换规则"],
            "rulesReplacedOrRemoved": [],
            "newRuleJustification": "",
            "expectedStructureEffect": "合并到现有规则，不新增章节",
        },
        "problemCaseIds": ["fail-1"],
        "neighborPassCaseIds": ["pass-1"],
        "boundaryPairs": [{
            "problemCaseId": "fail-1",
            "neighborPassCaseIds": ["pass-1"],
            "structuralDifference": "是否已经满足转换条件",
        }],
        "repeatRuns": 3,
        "unstableRepeatRuns": 5,
        "acceptancePolicy": {
            "schemaVersion": 1,
            "mode": "deterministic",
            "source": "user-confirmed",
            "successCriteria": "关键用例通过且保护集无不可接受退化",
            "criticalCaseIds": ["fail-1"],
            "protectedCaseIds": ["pass-1"],
        },
    }


class OptimizationJournalTest(unittest.TestCase):
    def make_run(self, root):
        run = os.path.join(root, "tasks", "demo", "runs", "run-1")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "prompt_v1.md"), "w", encoding="utf-8") as stream:
            stream.write("baseline prompt")
        plan_path = os.path.join(run, "optimization_iteration.json")
        write_json(plan_path, valid_plan())
        return run, plan_path

    def test_start_creates_new_prompt_version_without_overwriting_parent(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan_path = self.make_run(root)
            result = optimization_journal.start_attempt(
                run, "prompt_v1.md", plan_path, "缩短状态判断链")
            with open(os.path.join(run, "prompt_v1.md"), encoding="utf-8") as stream:
                parent = stream.read()
            with open(os.path.join(run, "prompt_v2.md"), encoding="utf-8") as stream:
                candidate = stream.read()
            with open(os.path.join(run, "rounds.json"), encoding="utf-8") as stream:
                events = json.load(stream)
        self.assertEqual("prompt_v2.md", result["version"])
        self.assertEqual("baseline prompt", parent)
        self.assertEqual(parent, candidate)
        self.assertEqual("attempt-started", events[0]["eventType"])
        self.assertEqual("首次状态转换边界不清", events[0]["rootCause"])
        self.assertEqual("merge-existing", events[0]["requirementIntegration"]["strategy"])

    def test_complete_appends_conclusion_updates_state_and_renders_resume_summary(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan_path = self.make_run(root)
            started = optimization_journal.start_attempt(
                run, "prompt_v1.md", plan_path, "缩短状态判断链")
            optimization_journal.complete_attempt(
                run, started["attemptId"], "rolled_back",
                "方向有效但保护集出现退化", "拆分例外条件后重新验证",
                round_name="candidate-v2", evidence={"fixed": 2, "broke": 1})
            with open(os.path.join(run, "rounds.json"), encoding="utf-8") as stream:
                events = json.load(stream)
            with open(os.path.join(run, "optimization-state.json"), encoding="utf-8") as stream:
                state = json.load(stream)
            with open(os.path.join(run, "OPTIMIZATION_SUMMARY.md"), encoding="utf-8") as stream:
                summary = stream.read()
        self.assertEqual(["attempt-started", "attempt-completed"],
                         [event["eventType"] for event in events])
        self.assertEqual("CANDIDATE_REJECTED_CONTINUE", state["status"])
        self.assertEqual("拆分例外条件后重新验证", state["nextAction"])
        self.assertIn("方向有效但保护集出现退化", summary)
        self.assertIn("拆分例外条件后重新验证", summary)
        self.assertIn("prompt_v2.md", summary)

    def test_completed_attempt_cannot_be_recorded_twice(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan_path = self.make_run(root)
            started = optimization_journal.start_attempt(run, "prompt_v1.md", plan_path, "边界修复")
            optimization_journal.complete_attempt(
                run, started["attemptId"], "accepted", "目标与保护集均通过", "生成正式报告")
            with self.assertRaisesRegex(ValueError, "已经完成"):
                optimization_journal.complete_attempt(
                    run, started["attemptId"], "accepted", "重复写入", "无")

    def test_accepted_attempt_updates_manifest_final_version(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan_path = self.make_run(root)
            write_json(os.path.join(run, "run_manifest.json"), {
                "schemaVersion": 1, "invocations": []})
            started = optimization_journal.start_attempt(run, "prompt_v1.md", plan_path, "边界修复")
            with open(os.path.join(run, started["version"]), "a", encoding="utf-8") as stream:
                stream.write("\nnew rule")
            optimization_journal.complete_attempt(
                run, started["attemptId"], "accepted", "全量达到任务标准", "生成正式报告")
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as stream:
                manifest = json.load(stream)
        self.assertEqual("prompt_v2.md", manifest["finalPrompt"])
        self.assertEqual("rounds.json", manifest["decisionLog"])

    def test_milestone_updates_summary_without_creating_prompt_version(self):
        with tempfile.TemporaryDirectory() as root:
            run, _ = self.make_run(root)
            optimization_journal.record_milestone(
                run, "case-correction", "用户批准修正3条错误用例", "重建完整基线")
            self.assertFalse(os.path.exists(os.path.join(run, "prompt_v2.md")))
            with open(os.path.join(run, "OPTIMIZATION_SUMMARY.md"), encoding="utf-8") as stream:
                summary = stream.read()
        self.assertIn("用户批准修正3条错误用例", summary)
        self.assertIn("重建完整基线", summary)

    def test_parent_prompt_must_be_inside_run_directory(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan_path = self.make_run(root)
            outside = os.path.join(root, "outside.md")
            with open(outside, "w", encoding="utf-8") as stream:
                stream.write("outside")
            with self.assertRaisesRegex(ValueError, "运行目录"):
                optimization_journal.start_attempt(run, outside, plan_path, "越界")


if __name__ == "__main__":
    unittest.main()
