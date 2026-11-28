import subprocess
import tempfile
import unittest
from pathlib import Path

import verifier_ratchet as vr

CHECK = "#!/usr/bin/env bash\nset -euo pipefail\ncargo test -q -p containment-kernel\nuv run tools/generate_proof.py --check\n"
LAWS = "law fail_closed:\n  x\nlaw tighten_only:\n  y\n"
TESTS = "#[test]\nfn lease_lapses() {}\n#[test]\n#[should_panic]\nfn reset_needs_a_signature() {}\n"


class Ratchet(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        self.write({"scripts/check.sh": CHECK, "formal/LAWS.bend": LAWS, "crates/k/src/lib.rs": TESTS})
        self.base = self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.dir), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            path = self.dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "x")
        return self.git("rev-parse", "HEAD")

    def dropped(self, waivers=None):
        path = None
        if waivers is not None:
            path = Path(tempfile.mkdtemp()) / "waivers.toml"
            path.write_text(waivers)
        return vr.dropped(vr.Repo(str(self.dir), self.base), vr.Repo(str(self.dir), self.commit()), path)

    def test_nothing_removed_passes(self):
        self.write({"formal/LAWS.bend": LAWS + "law clock_never_rewinds:\n  z\n"})
        self.assertEqual(self.dropped(), [])

    def test_a_removed_law_is_refused(self):
        self.write({"formal/LAWS.bend": "law fail_closed:\n  x\n"})
        self.assertEqual(self.dropped(), [("law", "tighten_only")])

    def test_a_removed_test_behind_another_attribute_is_refused(self):
        self.write({"crates/k/src/lib.rs": "#[test]\nfn lease_lapses() {}\n"})
        self.assertEqual(self.dropped(), [("rust test", "crates/k/src/lib.rs::reset_needs_a_signature")])

    def test_a_removed_check_command_is_refused_but_a_new_flag_is_not(self):
        self.write({"scripts/check.sh": "cargo test -q --locked -p containment-kernel\n"})
        self.assertEqual(self.dropped(), [("check.sh command", "uv run tools/generate_proof.py")])

    def test_an_announced_removal_passes_and_a_glob_covers_a_file(self):
        self.write({"crates/k/src/lib.rs": ""})
        self.assertEqual(self.dropped('[[removed]]\nkind = "rust test"\nname = "crates/k/src/lib.rs::*"\nreason = "the module moved"\n'), [])

    def test_a_removal_without_a_reason_is_refused(self):
        self.write({"formal/LAWS.bend": ""})
        with self.assertRaises(SystemExit):
            self.dropped('[[removed]]\nkind = "law"\nname = "*"\n')

    def test_a_waiver_written_in_the_change_itself_is_ignored(self):
        waiver = '[[removed]]\nkind = "law"\nname = "*"\nreason = "mine"\n'
        self.write({"formal/LAWS.bend": "", "ci/verifier-removals.toml": waiver, "waivers/verified-agent-containment.toml": waiver})
        self.assertEqual(self.dropped(), [("law", "fail_closed"), ("law", "tighten_only")])

    def test_the_waivers_file_may_hold_other_tables(self):
        self.write({"formal/LAWS.bend": "law fail_closed:\n  x\n"})
        waivers = ('[[expected]]\nstage = "*"\nbefore = "*"\nafter = "*"\nreason = "eval"\n'
                   '[[removed]]\nkind = "law"\nname = "tighten_only"\nreason = "merged into fail_closed"\n')
        self.assertEqual(self.dropped(waivers), [])

    def test_a_missing_waivers_file_is_bad_input(self):
        with self.assertRaises(SystemExit):
            vr.announced(Path(tempfile.mkdtemp()) / "missing.toml")

    def test_the_command_key_drops_flags_and_output(self):
        self.assertEqual(vr.command_key("bun tools/differential.mjs a b | tee results/d.json"), "bun tools/differential.mjs a b")
        self.assertEqual(vr.command_key("(cd formal && bend PROOF.bend --check-only)"), "formal bend PROOF.bend")


if __name__ == "__main__":
    unittest.main()
