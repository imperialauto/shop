"""Synthetic commissioning: coil-swap reasoning (eval cases 3 and 4).

Workstation QA on 2026-11-11 (docs/qa-evidence/local-node-qa-2026-11-11.md)
passed all four guardrail checks while the model read both swap cases
backwards. The "observed" fixtures below are reconstructed from that QA table
(top suspected cause, second cause, no repair). Everything is synthetic.
"""
import json
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient

from app import diagnostic_guard as guard
from app.main import app

NODE_KEY = "s" * 48
NODE = {"X-Local-Node-Key": NODE_KEY}
CASES = {c["name"]: c for c in guard.EVAL_CASES}
FOLLOWED = CASES["confirmed_finding_supports_repair"]
STAYED = CASES["swap_fault_stayed"]


def swap_draft(case, causes, tests=None, repairs=None, cautions=None, missing=None):
    note = case["snapshot"]["technician_notes"]
    return {
        "customer_reported_symptoms": ["Rough idle", "Check engine light on"],
        "reported_codes": [{"code": "P0301", "status_as_supplied": "active", "meaning": "Cylinder 1 misfire detected"}],
        "technician_confirmed_findings": [note],
        "suspected_causes": [{"cause": c, "basis": b, "likelihood": lk} for c, b, lk in causes],
        "recommended_tests": [{"test": t, "purpose": p} for t, p in (tests or [])],
        "proposed_repairs": [{"repair": r, "justified_by_confirmed_findings": [note]} for r in (repairs or [])],
        "missing_information": missing or ["Freeze frame data"],
        "cautions": cautions or [],
    }


FOLLOWED_BASIS = "Misfire followed the coil to cylinder 3 after the coil 1/coil 3 swap"
STAYED_BASIS = "Misfire stayed on cylinder 1 after the coil 1/coil 3 swap"

# --- What the workstation model actually produced (must be REJECTED) -------
OBSERVED_FOLLOWED = {
    "observed: coil 3 named": (swap_draft(FOLLOWED, [
        ("Faulty ignition coil 3", FOLLOWED_BASIS, "high"),
        ("Wiring or connector fault at cylinder 3", FOLLOWED_BASIS, "medium")]), ["swap_wrong_coil_blamed"]),
    "observed: coil in cylinder 3": (swap_draft(FOLLOWED, [
        ("Faulty ignition coil in cylinder 3", FOLLOWED_BASIS, "high"),
        ("Cylinder 3 wiring or connector fault", FOLLOWED_BASIS, "medium")]), ["swap_coil_position_ambiguous"]),
}
OBSERVED_STAYED = {
    "observed: cylinder 1 coil ranked high": (swap_draft(STAYED, [
        ("Cylinder 1 ignition coil faulty", STAYED_BASIS, "high"),
        ("Wiring or connector fault at cylinder 1", STAYED_BASIS, "medium")],
        tests=[("Inspect cylinder 1 coil connector and wiring harness", "Check for a poor connection")]),
        ["swap_ruled_out_coil_ranked"]),
}

# --- Other wrong answers the grader must catch ------------------------------
WRONG_FOLLOWED = {
    "coil now in cylinder 1 blamed": (swap_draft(FOLLOWED, [
        ("Ignition coil now installed in cylinder 1", FOLLOWED_BASIS, "high")]), ["swap_wrong_coil_blamed"]),
    "original cylinder 3 coil blamed": (swap_draft(FOLLOWED, [
        ("The original cylinder 3 coil has an internal fault", FOLLOWED_BASIS, "high")]), ["swap_wrong_coil_blamed"]),
    "wiring ranked first": (swap_draft(FOLLOWED, [
        ("Cylinder 3 injector wiring fault", FOLLOWED_BASIS, "high"),
        ("Original cylinder 1 coil", FOLLOWED_BASIS, "medium")]), ["swap_moved_coil_not_top_ranked"]),
    "tie with wiring": (swap_draft(FOLLOWED, [
        ("Original cylinder 1 coil", FOLLOWED_BASIS, "high"),
        ("Cylinder 3 wiring fault", FOLLOWED_BASIS, "high")]), ["swap_moved_coil_not_top_ranked"]),
    "no causes at all": (swap_draft(FOLLOWED, []), ["swap_moved_coil_not_identified"]),
    "right coil, wrong repair": (swap_draft(FOLLOWED, [
        ("Coil originally in cylinder 1 (now in cylinder 3)", FOLLOWED_BASIS, "high")],
        repairs=["Replace the ignition coil now in cylinder 1"]), ["swap_repair_wrong_coil"]),
    "right coil, ambiguous repair": (swap_draft(FOLLOWED, [
        ("Coil originally in cylinder 1 (now in cylinder 3)", FOLLOWED_BASIS, "high")],
        repairs=["Replace the cylinder 3 ignition coil"]), ["swap_repair_coil_ambiguous"]),
}
WRONG_STAYED = {
    "coil still medium behind plug": (swap_draft(STAYED, [
        ("Worn spark plug, cylinder 1", STAYED_BASIS, "high"),
        ("Ignition coil 1 intermittent", STAYED_BASIS, "medium")],
        tests=[("Pull and inspect the cylinder 1 spark plug", "Look for fouling or a cracked insulator")]),
        ["swap_ruled_out_coil_ranked"]),
    "coil replacement proposed": (swap_draft(STAYED, [
        ("Spark plug fault on cylinder 1", STAYED_BASIS, "high")],
        tests=[("Inspect the cylinder 1 spark plug", "Check gap and condition")],
        repairs=["Replace ignition coil 1"]), ["swap_ruled_out_coil_replaced"]),
    "coil replacement as a test": (swap_draft(STAYED, [
        ("Spark plug fault on cylinder 1", STAYED_BASIS, "high")],
        tests=[("Replace the cylinder 1 coil and road test", "Confirm the misfire is gone"),
               ("Inspect the cylinder 1 spark plug", "Check gap and condition")]), ["swap_ruled_out_coil_replaced"]),
    "no cylinder 1 checks": (swap_draft(STAYED, [
        ("Spark plug fault", STAYED_BASIS, "medium")],
        tests=[("Check misfire counters on all cylinders", "See if the misfire spreads")]),
        ["swap_missing_cylinder_checks"]),
}

# --- Correct answers in different wording (must be ACCEPTED) ----------------
CORRECT_FOLLOWED = {
    "original cyl 1 coil, now in 3": swap_draft(FOLLOWED, [
        ("Original cylinder 1 ignition coil (now installed in cylinder 3)", FOLLOWED_BASIS, "high"),
        ("Cylinder 3 wiring or connector", FOLLOWED_BASIS, "low")]),
    "coil #1 moved to cylinder 3": swap_draft(FOLLOWED, [
        ("Ignition coil #1 — the one moved to cylinder 3", FOLLOWED_BASIS, "high")]),
    "came from cylinder 1": swap_draft(FOLLOWED, [
        ("The coil that came from cylinder 1, currently on cylinder 3, is failing", FOLLOWED_BASIS, "high"),
        ("Coil originally in cylinder 3 (now in cylinder 1)", "Did not misfire in either position", "low")]),
    "originally fitted to cyl 1": swap_draft(FOLLOWED, [
        ("Defective coil originally fitted to cyl 1", FOLLOWED_BASIS, "medium"),
        ("Spark plug, cylinder 3", "Less likely because the misfire moved with the coil", "low")]),
    "with a repair": swap_draft(FOLLOWED, [
        ("Coil from cylinder 1 is faulty; it took the misfire with it", FOLLOWED_BASIS, "high")],
        repairs=["Replace the original cylinder 1 coil (now in cylinder 3)"]),
}
CORRECT_STAYED = {
    "plug first, coil ruled out": swap_draft(STAYED, [
        ("Spark plug fault, cylinder 1", STAYED_BASIS, "high"),
        ("Cylinder 1 injector or injector wiring", STAYED_BASIS, "medium"),
        ("Ignition coil — ruled out by the swap", STAYED_BASIS, "low")],
        tests=[("Inspect and swap the cylinder 1 spark plug", "See if the misfire follows the plug"),
               ("Noid light and injector balance test on cylinder 1", "Check injector drive and flow"),
               ("Compression test on cylinder 1", "Rule out a mechanical fault")],
        cautions=["Do not replace the coil; the swap ruled it out."]),
    "mechanical wording, no coil": swap_draft(STAYED, [
        ("Fouled or cracked plug on cyl 1", STAYED_BASIS, "high"),
        ("Mechanical issue (compression or valve) on #1", STAYED_BASIS, "medium")],
        tests=[("Run a compression and leak-down test on cyl #1", "Find a mechanical cause")],
        missing=["Spark plug condition for cylinder 1"]),
}


class SwapGradingTests(unittest.TestCase):
    def grade(self, case, draft):
        return guard.validate_eval_case(json.dumps(draft), case)

    def assert_rejected(self, case, fixtures):
        for label, (draft, expected) in fixtures.items():
            with self.subTest(label):
                plain = guard.validate_output(json.dumps(draft), case["snapshot"])
                self.assertTrue(plain["ok"], f"hard rules should pass, isolate the swap check: {plain['violations']}")
                verdict = self.grade(case, draft)
                self.assertFalse(verdict["ok"])
                self.assertIsNone(verdict["draft"])
                self.assertEqual(verdict["violations"], expected)

    def assert_accepted(self, case, fixtures):
        for label, draft in fixtures.items():
            with self.subTest(label):
                verdict = self.grade(case, draft)
                self.assertTrue(verdict["ok"], verdict["violations"])

    def test_observed_followed_answers_rejected(self):
        self.assert_rejected(FOLLOWED, OBSERVED_FOLLOWED)

    def test_observed_stayed_answer_rejected(self):
        self.assert_rejected(STAYED, OBSERVED_STAYED)

    def test_other_wrong_followed_answers_rejected(self):
        self.assert_rejected(FOLLOWED, WRONG_FOLLOWED)

    def test_other_wrong_stayed_answers_rejected(self):
        self.assert_rejected(STAYED, WRONG_STAYED)

    def test_correct_followed_answers_accepted(self):
        self.assert_accepted(FOLLOWED, CORRECT_FOLLOWED)

    def test_correct_stayed_answers_accepted(self):
        self.assert_accepted(STAYED, CORRECT_STAYED)

    def test_a_correct_answer_for_one_case_fails_the_other(self):
        followed_right = CORRECT_FOLLOWED["original cyl 1 coil, now in 3"]
        self.assertIn("swap_ruled_out_coil_ranked", self.grade(STAYED, followed_right)["violations"])
        stayed_right = CORRECT_STAYED["plug first, coil ruled out"]
        self.assertIn("swap_moved_coil_not_identified", self.grade(FOLLOWED, stayed_right)["violations"])

    def test_hard_rule_failures_still_reported(self):
        draft = CORRECT_FOLLOWED["original cyl 1 coil, now in 3"]
        draft = {**draft, "cautions": ["A new coil costs about $85"]}
        self.assertEqual(self.grade(FOLLOWED, draft)["violations"], ["price_mentioned"])
        self.assertEqual(guard.validate_eval_case("not json", FOLLOWED)["violations"], ["not_json"])

    def test_coil_origin_reading(self):
        for text, origins in (("Original cylinder 1 coil", {1}), ("coil now in cylinder 3", {1}),
                              ("coil now in cylinder 1", {3}), ("coil 3", {3}), ("coil #1", {1}),
                              ("cylinder 3 coil", set()), ("coil in cylinder 3", set()),
                              ("the original coil now in cylinder 3", {1}),
                              ("coil 1 (now in cylinder 3)", {1}), ("coil 3 (now in cylinder 3)", {1, 3})):
            self.assertEqual(guard.coil_origins(text, 1, 3), origins, text)


class ProductionPathUnchangedTests(unittest.TestCase):
    """Real job results are NOT graded on swap reasoning; the technician is."""

    def test_validate_output_ignores_swap_reasoning(self):
        snapshot = FOLLOWED["snapshot"]
        draft, _ = OBSERVED_FOLLOWED["observed: coil 3 named"]
        verdict = guard.validate_output(json.dumps(draft), snapshot)
        self.assertTrue(verdict["ok"], verdict["violations"])
        self.assertTrue(any("swap test" in c for c in verdict["cautions"]))

    def test_real_cases_never_carry_a_swap_answer(self):
        self.assertEqual({c["name"] for c in guard.EVAL_CASES if c.get("swap")},
                         {"confirmed_finding_supports_repair", "swap_fault_stayed"})
        for case in guard.EVAL_CASES:
            self.assertTrue(case["snapshot"]["job_reference"].startswith("EVAL-"))


class ValidateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY})
        self.env.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        self.client = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.read_only
        self.env.stop()

    def post(self, case, draft):
        response = self.client.post("/internal/local-node/validate", headers=NODE,
                                    json={"case": case["name"], "output": json.dumps(draft)})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_validate_rejects_observed_swap_answers(self):
        followed = self.post(FOLLOWED, OBSERVED_FOLLOWED["observed: coil 3 named"][0])
        self.assertEqual((followed["ok"], followed["violations"]), (False, ["swap_wrong_coil_blamed"]))
        stayed = self.post(STAYED, OBSERVED_STAYED["observed: cylinder 1 coil ranked high"][0])
        self.assertEqual((stayed["ok"], stayed["violations"]), (False, ["swap_ruled_out_coil_ranked"]))

    def test_validate_accepts_correct_swap_answers(self):
        self.assertTrue(self.post(FOLLOWED, CORRECT_FOLLOWED["coil #1 moved to cylinder 3"])["ok"])
        self.assertTrue(self.post(STAYED, CORRECT_STAYED["mechanical wording, no coil"])["ok"])


if __name__ == "__main__":
    unittest.main()
