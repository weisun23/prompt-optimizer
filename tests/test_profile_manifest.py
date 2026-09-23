# -*- coding: utf-8 -*-
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import model_profiles
import profile_constraints
import report_gen
import runner


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ProfileManifestTest(unittest.TestCase):
    def make_run(self, root, validation_verdict="PASS", prompt_hash=None,
                 include_constraints=True):
        run = os.path.join(root, "tasks", "demo", "runs", "run-profile")
        out = os.path.join(run, "trajectories-qwen")
        os.makedirs(run, exist_ok=True)
        config_path = os.path.join(root, "config.json")
        task_path = os.path.join(root, "task.json")
        cases_path = os.path.join(root, "cases.json")
        prompt_path = os.path.join(run, "prompt_v2.md")
        constraints_path = os.path.join(run, "optimization_constraints.json")
        validation_path = os.path.join(run, "constraint_validation.json")
        config = {"models": {"candidate": {
            "model": "Qwen3.8-27B", "provider": "qwen", "modelFamily": "qwen3",
            "apiStyle": "openai-chat", "promptProfile": "auto", "reasoning": False,
            "toolChoicePolicy": "required", "capabilities": {"nativeTools": True},
        }}}
        write_json(config_path, config)
        write_json(task_path, {"initialPrompt": "fallback", "simulateMode": "off",
                              "promptTaskType": "tool-agent"})
        write_json(cases_path, [{
            "id": "case-1", "chat": {"simulateMode": "off", "messages": [
                {"role": "user", "content": "hi"}
            ]}
        }])
        with open(prompt_path, "w", encoding="utf-8") as stream:
            stream.write("system")
        registry = model_profiles.load_registry()
        profile = next(item for item in registry["profiles"]
                       if item["id"] == "qwen-nonthinking")
        snapshot = profile_constraints.build_constraint_snapshot(
            profile, "accepted", ["QWEN-NONTHINK-01"], {},
            accepted_at="2026-09-08T10:00:00Z",
            principle_ids=["PRINCIPLE-GLOBAL-REVIEW"], principles_confirmed=True)
        write_json(constraints_path, snapshot)
        write_json(validation_path, {
            "schemaVersion": 1,
            "verdict": validation_verdict,
            "profileId": snapshot["profileId"],
            "promptSha256": prompt_hash or hashlib.sha256(b"system").hexdigest(),
            "constraintsSha256": canonical_hash(snapshot),
            "validatedAt": "2026-09-15T10:00:00Z",
            "staticErrors": [],
            "judge": {"model": "judge-model", "provider": "test"},
            "results": [{"id": item["id"], "verdict": "PASS", "evidence": "已核验"}
                        for item in snapshot["hardConstraints"]],
        })
        argv = [
            "runner.py", "--config", config_path, "--task", task_path,
            "--prompt", prompt_path, "--cases", cases_path, "--out", out,
            "--run-id", "qwen-profile", "--run-kind", "full",
        ]
        if include_constraints:
            argv.extend(["--constraints", constraints_path,
                         "--constraint-validation", validation_path])
        return run, out, argv

    def test_execution_manifest_records_profile_and_constraint_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, out, argv = self.make_run(tmp)
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", return_value=[]):
                runner.main()
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as stream:
                row = json.load(stream)["invocations"][0]
            with open(os.path.join(out, "_run_info.json"), encoding="utf-8") as stream:
                run_info = json.load(stream)
        self.assertEqual("qwen-nonthinking", row["model"]["promptProfile"])
        self.assertEqual("high", row["model"]["promptProfileConfidence"])
        self.assertEqual("required", row["model"]["toolChoicePolicy"])
        self.assertEqual("tool-agent", row["model"]["promptTaskType"])
        self.assertTrue(row["model"]["recommendedDirectiveIds"])
        self.assertRegex(row["constraints"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(["PRINCIPLE-GLOBAL-REVIEW"], row["constraints"]["principleIds"])
        self.assertEqual(["QWEN-NONTHINK-01"], row["constraints"]["modelDirectiveIds"])
        self.assertEqual("PASS", row["constraintValidation"]["verdict"])
        self.assertEqual("Qwen 非思考", run_info["candidateProfileLabel"])

    def test_nonpass_or_stale_constraint_validation_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, argv = self.make_run(tmp, validation_verdict="FAIL")
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", side_effect=AssertionError("不应执行")):
                with self.assertRaisesRegex(SystemExit, "硬约束验证未通过"):
                    runner.main()
        with tempfile.TemporaryDirectory() as tmp:
            _, _, argv = self.make_run(tmp, prompt_hash="0" * 64)
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", side_effect=AssertionError("不应执行")):
                with self.assertRaisesRegex(SystemExit, "候选提示词哈希不一致"):
                    runner.main()

    def test_incomplete_constraint_receipt_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, argv = self.make_run(tmp)
            validation_path = argv[argv.index("--constraint-validation") + 1]
            write_json(validation_path, {"schemaVersion": 1, "verdict": "PASS"})
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", side_effect=AssertionError("不应执行")):
                with self.assertRaisesRegex(SystemExit, "回执无效"):
                    runner.main()

    def test_pending_case_correction_requires_full_rebaseline_and_completes_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, out, argv = self.make_run(tmp, include_constraints=False)
            case_root = os.path.join(tmp, "cases")
            write_json(os.path.join(case_root, "case-1.json"), {
                "id": "case-1", "chat": {"simulateMode": "off", "messages": [
                    {"role": "user", "content": "hi"}
                ]}
            })
            case_hash = runner.case_correction_guard.case_set_hash(case_root)
            marker_path = os.path.join(case_root, ".rebaseline-required.json")
            receipt_path = os.path.join(tmp, "case-correction-receipt.json")
            write_json(marker_path, {"schemaVersion": 1, "status": "pending",
                       "caseSetSha256": case_hash, "appliedCaseIds": ["case-1"],
                       "caseCount": 1, "caseIds": ["case-1"]})
            write_json(receipt_path, {"schemaVersion": 1, "requiresRebaseline": True,
                       "caseSetSha256": case_hash, "appliedCaseIds": ["case-1"],
                       "caseCount": 1, "caseIds": ["case-1"]})
            cases_index = argv.index("--cases") + 1
            argv[cases_index] = case_root
            argv.extend(["--case-correction-receipt", receipt_path])
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", return_value=[]):
                runner.main()
            with open(marker_path, encoding="utf-8") as stream:
                marker = json.load(stream)
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as stream:
                record = json.load(stream)["invocations"][0]
        self.assertEqual("completed", marker["status"])
        self.assertEqual(case_hash, record["caseCorrection"]["caseSetSha256"])
        self.assertEqual("completed", record["rebaseline"]["status"])

    def test_constraint_snapshot_task_type_must_match_current_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, argv = self.make_run(tmp)
            task_path = argv[argv.index("--task") + 1]
            write_json(task_path, {"initialPrompt": "fallback", "simulateMode": "off",
                                   "promptTaskType": "classification"})
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", side_effect=AssertionError("不应执行")):
                with self.assertRaisesRegex(SystemExit, "任务类型不一致"):
                    runner.main()

    def test_unconfirmed_auto_profile_is_recommended_but_not_claimed_as_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _, argv = self.make_run(tmp, include_constraints=False)
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", return_value=[]):
                runner.main()
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as stream:
                model = json.load(stream)["invocations"][0]["model"]
        self.assertEqual("generic", model["promptProfile"])
        self.assertEqual("qwen-nonthinking", model["recommendedProfile"])

    def test_runner_passes_task_type_into_profile_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _, argv = self.make_run(tmp, include_constraints=False)
            task_path = argv[argv.index("--task") + 1]
            write_json(task_path, {"initialPrompt": "fallback", "simulateMode": "off",
                                   "promptTaskType": "classification"})
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(runner, "run_all", return_value=[]):
                runner.main()
            with open(os.path.join(run, "run_manifest.json"), encoding="utf-8") as stream:
                model = json.load(stream)["invocations"][0]["model"]
        self.assertEqual("classification", model["promptTaskType"])
        self.assertIn("TASK-CLASSIFICATION-01", model["recommendedDirectiveIds"])
        self.assertNotIn("QWEN-RUNTIME-02", model["runtimeRequirementIds"])

    def test_report_profile_section_exposes_selection_and_validation(self):
        html = report_gen.profile_section({
            "model": {
                "profileLabel": "Qwen 非思考",
                "promptProfile": "qwen-nonthinking",
                "promptProfileConfidence": "high",
            },
            "constraints": {"status": "accepted", "count": 4},
            "constraintValidation": {"verdict": "PASS"},
        })
        self.assertIn("Qwen 非思考", html)
        self.assertIn("用户已接受", html)
        self.assertIn("硬约束验证：通过", html)

    def test_score_curve_keeps_model_and_profile_scope_per_round(self):
        rows = report_gen.score_rows({
            "qwen": {"a": {"caseId": "a", "totalScore": 100, "verdict": "PASS"}},
            "deepseek": {"a": {"caseId": "a", "totalScore": 100, "verdict": "PASS"}},
        }, [
            {"id": "qwen", "kind": "full", "model": {
                "model": "Qwen3.8-27B", "promptProfile": "qwen-nonthinking"
            }},
            {"id": "deepseek", "kind": "full", "model": {
                "model": "deepseek-v4-pro", "promptProfile": "deepseek-thinking"
            }},
        ])
        self.assertEqual("Qwen3.8-27B", rows[0]["model"])
        self.assertEqual("qwen-nonthinking", rows[0]["profile"])
        self.assertEqual("deepseek-v4-pro", rows[1]["model"])
        self.assertEqual("deepseek-thinking", rows[1]["profile"])


if __name__ == "__main__":
    unittest.main()
