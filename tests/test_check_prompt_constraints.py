# -*- coding: utf-8 -*-
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import check_prompt
import model_profiles
import profile_constraints


class CheckPromptConstraintTest(unittest.TestCase):
    def integration_plan(self, strategy="merge-existing"):
        return {"requirementIntegration": {
            "strategy": strategy,
            "existingRuleLocations": ["## 状态规则"] if strategy != "new-local-rule" else [],
            "targetLocations": ["## 状态规则"],
        }}

    def test_integration_locations_must_resolve_in_prompt(self):
        old = "# 系统\n\n## 状态规则\n旧规则"
        new = "# 系统\n\n## 状态规则\n合并后的规则"
        self.assertEqual([], check_prompt.integration_location_errors(
            self.integration_plan(), old, new))
        errors = "\n".join(check_prompt.integration_location_errors(
            self.integration_plan(), "# 系统", "# 系统"))
        self.assertIn("existingRuleLocations", errors)
        self.assertIn("targetLocations", errors)

    def test_allow_shrink_requires_confirmed_concise_principle(self):
        registry = model_profiles.load_registry()
        profile = next(item for item in registry["profiles"] if item["id"] == "generic")
        unselected_snapshot = profile_constraints.build_constraint_snapshot(
            profile, "accepted", [], {}, task_type="generic",
            principle_ids=["PRINCIPLE-GLOBAL-REVIEW"], principles_confirmed=True)
        selected_snapshot = profile_constraints.build_constraint_snapshot(
            profile, "accepted", [], {}, task_type="generic",
            principle_ids=["PRINCIPLE-CONCISE"], principles_confirmed=True)
        missing = check_prompt.shrink_authorization_errors(True, None)
        unselected = check_prompt.shrink_authorization_errors(True, unselected_snapshot)
        selected = check_prompt.shrink_authorization_errors(True, selected_snapshot)
        self.assertTrue(missing)
        self.assertTrue(unselected)
        self.assertEqual([], selected)

    def test_no_shrink_flag_needs_no_principle_authorization(self):
        self.assertEqual([], check_prompt.shrink_authorization_errors(False, None))


if __name__ == "__main__":
    unittest.main()
