import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import eval_diff


def case(name, gate="allowed", outcome="succeeded", final_mode="run", started=True, modes=()):
    requests = [] if gate is None else [{"rid": "1.0", "phase": "injection", "tool": "send_email", "gate": gate,
                                         "gate_error": "host or method is not in the policy" if gate == "refused" else None}]
    return {"case": name, "program": {"started": started}, "requests": requests, "final_mode": final_mode,
            "mode_changes": list(modes), "outcome": outcome, "agentdojo": {"injection_succeeded": True}}


class EvalDiff(unittest.TestCase):
    def run_dirs(self, before, after):
        tmp = Path(tempfile.mkdtemp())
        for name, rows in (("before", before), ("after", after)):
            (tmp / name).mkdir()
            (tmp / name / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return eval_diff.load(tmp / "before"), eval_diff.load(tmp / "after")

    def test_identical_runs_have_no_fingerprints(self):
        a, b = self.run_dirs([case("slack.u0.i0")], [case("slack.u0.i0")])
        self.assertEqual(eval_diff.compare(a, b, []), [])

    def test_one_cause_is_one_group(self):
        rows = [f"workspace.u{i}.i0" for i in range(5)]
        a, b = self.run_dirs([case(n) for n in rows], [case(n, gate="refused", outcome="blocked") for n in rows])
        groups = eval_diff.compare(a, b, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["cases"], 5)
        self.assertEqual(groups[0]["stage"], "gate on injection send_email")

    def test_the_first_stage_that_differs_names_the_group(self):
        lapse = [{"from": "run", "to": "freeze", "event": "lease_clock"}, {"from": "freeze", "to": "kill", "event": "lease_clock"}]
        a, b = self.run_dirs([case("slack.u0.i4")],
                             [case("slack.u0.i4", gate=None, outcome="error", final_mode="kill", modes=lapse)])
        [group] = eval_diff.compare(a, b, [])
        self.assertEqual((group["stage"], group["before"], group["after"]), ("request count", 1, 0))

    def test_a_missing_case_is_a_difference(self):
        a, b = self.run_dirs([case("banking.u0.i0"), case("banking.u1.i0")], [case("banking.u0.i0")])
        [group] = eval_diff.compare(a, b, [])
        self.assertEqual(group["stage"], "case present")

    def test_an_expected_difference_is_marked_and_others_are_not(self):
        rule = {"suite": "workspace", "stage": "gate on *", "before": '"allowed"', "after": '"refused (*)"', "reason": "by address"}
        a, b = self.run_dirs([case("workspace.u0.i0"), case("travel.u0.i0")],
                             [case("workspace.u0.i0", gate="refused", outcome="blocked"), case("travel.u0.i0", gate="refused", outcome="blocked")])
        groups = {g["suite"]: g["expected"] for g in eval_diff.compare(a, b, [rule])}
        self.assertEqual(groups, {"workspace": "by address", "travel": None})

    def test_a_rule_without_a_reason_is_refused(self):
        tmp = Path(tempfile.mkdtemp()) / "x.toml"
        tmp.write_text('[[expected]]\nstage = "*"\nbefore = "*"\nafter = "*"\n')
        with self.assertRaises(SystemExit):
            eval_diff.expected_rules(tmp)

    def test_the_waivers_file_may_hold_other_tables(self):
        tmp = Path(tempfile.mkdtemp()) / "waivers.toml"
        tmp.write_text('[[removed]]\nkind = "law"\nname = "x"\nreason = "ratchet"\n'
                       '[[expected]]\nstage = "*"\nbefore = "*"\nafter = "*"\nreason = "meant"\n')
        [rule] = eval_diff.expected_rules(tmp)
        self.assertEqual(rule["reason"], "meant")

    def test_no_waivers_file_in_this_repository(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("evals/agentdojo/expected-differences.toml", "ci/verifier-removals.toml", "waivers"):
            self.assertFalse((root / name).exists(), f"{name}: waivers live in the organization's .github repository")

    def test_the_old_flag_still_reads_the_waivers(self):
        a, b = self.run_dirs([case("workspace.u0.i0")], [case("workspace.u0.i0", gate="refused", outcome="blocked")])
        tmp = Path(tempfile.mkdtemp()) / "waivers.toml"
        tmp.write_text('[[expected]]\nstage = "gate on *"\nbefore = "*"\nafter = "*"\nreason = "meant"\n')
        rows = [json.dumps(case("workspace.u0.i0")), json.dumps(case("workspace.u0.i0", gate="refused", outcome="blocked"))]
        dirs = Path(tempfile.mkdtemp())
        for name, row in zip(("before", "after"), rows):
            (dirs / name).mkdir()
            (dirs / name / "cases.jsonl").write_text(row + "\n")
        for flag in ("--waivers", "--expected"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(eval_diff.main([str(dirs / "before"), str(dirs / "after"), flag, str(tmp)]), 0)


if __name__ == "__main__":
    unittest.main()
