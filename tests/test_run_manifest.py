# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import run_manifest


class RunManifestTest(unittest.TestCase):
    def test_execution_records_are_preserved_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "run_manifest.json")
            run_manifest.record_execution(manifest, {
                "id": "baseline", "kind": "full",
                "trajectoryDir": "trajectories-baseline",
            })
            run_manifest.record_execution(manifest, {
                "id": "regress2", "kind": "regression",
                "trajectoryDir": "trajectories-regress2",
            })
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual(1, data["schemaVersion"])
        self.assertEqual(["baseline", "regress2"], [x["id"] for x in data["invocations"]])

    def test_same_id_is_updated_without_losing_other_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "run_manifest.json")
            run_manifest.record_execution(manifest, {"id": "a", "kind": "full", "trajectoryDir": "ta"})
            run_manifest.record_execution(manifest, {"id": "b", "kind": "full", "trajectoryDir": "tb"})
            run_manifest.record_execution(manifest, {"id": "a", "kind": "stability", "trajectoryDir": "ta2"})
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual(2, len(data["invocations"]))
        self.assertEqual("stability", next(x for x in data["invocations"] if x["id"] == "a")["kind"])

    def test_scoring_attaches_to_matching_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "run_manifest.json")
            run_manifest.record_execution(manifest, {
                "id": "baseline", "kind": "full",
                "trajectoryDir": "trajectories-baseline",
            })
            run_manifest.record_scoring(
                manifest, "trajectories-baseline", "scores/baseline",
                scorer_name="score_tool_binary.py", score_mode="tool-binary",
                scorer_version="1.2.0",
            )
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
        row = data["invocations"][0]
        self.assertEqual("scores/baseline", row["scoreDir"])
        self.assertEqual("tool-binary", row["scoreMode"])
        self.assertEqual("score_tool_binary.py", row["scorer"]["name"])

    def test_hash_snapshots_capture_files_and_json_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prompt.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("hello")
            file_info = run_manifest.file_snapshot(path, tmp)
            value_info = run_manifest.value_snapshot({"b": 2, "a": 1})
        self.assertEqual("prompt.md", file_info["path"])
        self.assertEqual(64, len(file_info["sha256"]))
        self.assertEqual(64, len(value_info["sha256"]))

    def test_find_run_root_under_scores_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            score_dir = os.path.join(tmp, "scores", "round1")
            os.makedirs(score_dir)
            self.assertEqual(os.path.abspath(tmp), run_manifest.find_run_root(score_dir))

    def test_scorers_accept_manifest_and_run_id_flags(self):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        for script in ("scorer.py", "score_tool_binary.py"):
            result = subprocess.run(
                [sys.executable, os.path.join(BIN, script), "--help"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("--manifest", result.stdout)
            self.assertIn("--run-id", result.stdout)

    def test_scorer_ignores_trajectory_metadata_files(self):
        import scorer

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "case-1.json"), "w", encoding="utf-8") as f:
                json.dump({"caseId": "case-1", "messages": []}, f)
            with open(os.path.join(tmp, "_run_info.json"), "w", encoding="utf-8") as f:
                json.dump({"candidateModel": "demo"}, f)
            rows = scorer.load_trajectories(tmp)
        self.assertEqual(["case-1"], [row["caseId"] for row in rows])


if __name__ == "__main__":
    unittest.main()
