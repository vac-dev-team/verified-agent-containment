#!/usr/bin/env python3
"""Refuse a change that drops a verifier without saying so.

Lists what does the verifying at two git revisions: the commands in
scripts/check.sh, the laws, the kernel mutants, the Kani harnesses and their
mutants, the Rust tests, the attack and live scenarios, the invariant cases and
the tool tests. Anything at BASE that is gone at HEAD must be named, with a
reason, under [[removed]] in the waivers file, or the check fails. Adding is
always fine. A renamed test counts as one removed and one added. A name in the
waivers file may be a glob, such as a whole deleted test file.

The waivers file is waivers/verified-agent-containment.toml in the
organization's .github repository, next to the required check that runs this.
Agents do not edit that repository, so a change cannot waive its own removal.
Nothing in this repository is read for waivers. Without --waivers, no removal
is waived.

Run the copy from the base branch, not the one in the change under review, so a
change cannot edit the rule that judges it. The required check does:

    git show "$BASE:tools/verifier_ratchet.py" > "$RUNNER_TEMP/verifier_ratchet.py"
    python3 -I "$RUNNER_TEMP/verifier_ratchet.py" "$BASE" HEAD --waivers ../org/waivers/verified-agent-containment.toml

usage: verifier_ratchet.py BASE [HEAD] [--repo DIR] [--waivers FILE]
Exit 0 when nothing was dropped unannounced, 1 when something was, 2 on bad input.
"""
import argparse
import ast
import fnmatch
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CHECK_COMMAND = re.compile(r"^(uv |cargo |bun |node |bend |\(cd |tools/|scripts/|target/)")
LAW = re.compile(r"^law (\w+):", re.M)
TEST = re.compile(r"#\[test\]\s*(?:#\[[^\]]*\]\s*)*fn (\w+)")
KANI = re.compile(r"#\[kani::proof\]\s*(?:#\[[^\]]*\]\s*)*fn (\w+)")
SCENARIOS = re.compile(r"SCENARIOS: &\[&str\] = &\[(.*?)\];", re.S)
PY_TEST = re.compile(r"^\s*def (test_\w+)", re.M)


class Repo:
    def __init__(self, path, rev):
        self.path, self.rev = path, rev
        self.files = self._git("ls-tree", "-r", "--name-only", rev).splitlines()

    def _git(self, *args):
        run = subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True, encoding="utf-8")
        if run.returncode:
            raise SystemExit(f"verifier_ratchet: git {' '.join(args)}: {run.stderr.strip()}")
        return run.stdout

    def read(self, name):
        return self._git("show", f"{self.rev}:{name}") if name in self.files else ""

    def glob(self, prefix, suffix):
        return [f for f in self.files if f.startswith(prefix) and f.endswith(suffix)]


def command_key(line):
    """A check.sh command without its flags or where its output goes, so adding
    --locked or moving a report does not read as a removed check."""
    words = line.split("|")[0].replace("(", " ").replace(")", " ").split()
    return " ".join(w for w in words if not w.startswith("-") and w not in ("cd", "&&", ">", ">/dev/null"))


def mutant_names(source, variable):
    for node in ast.parse(source or "x = 0").body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == variable for t in node.targets):
            return [ast.literal_eval(item.elts[0]) for item in node.value.elts]
    return []


def inventory(repo):
    """{kind: set of names} at one revision."""
    out = {
        "check.sh command": {command_key(l) for l in repo.read("scripts/check.sh").splitlines() if CHECK_COMMAND.match(l.strip())},
        "law": set(LAW.findall(repo.read("formal/LAWS.bend"))),
        "bend mutant": set(mutant_names(repo.read("tools/kernel_mutants.py"), "BEND_MUTANTS")),
        "rust mutant": set(mutant_names(repo.read("tools/kernel_mutants.py"), "RUST_MUTANTS")),
        "kani mutant": {m["name"] for m in tomllib.loads(repo.read("formal/kani/mutants.toml") or "").get("mutant", [])},
        "kani harness": set(), "rust test": set(), "scenario": set(), "tool test": set(),
        "invariant case": {f.split("/")[2] for f in repo.glob("tools/invariant_cases/", "") if f.count("/") >= 3},
    }
    for name in repo.glob("formal/kani/", ".rs"):
        out["kani harness"] |= set(KANI.findall(repo.read(name)))
    for name in repo.glob("crates/", ".rs"):
        text = repo.read(name)
        out["rust test"] |= {f"{name}::{t}" for t in TEST.findall(text)}
        for block in SCENARIOS.findall(text):
            out["scenario"] |= set(re.findall(r'"([^"]+)"', block))
    for name in repo.glob("tools/test_", ".py"):
        out["tool test"] |= {f"{name}::{t}" for t in PY_TEST.findall(repo.read(name))}
    return out


def announced(waivers):
    """[(kind, name)] from the waivers file's [[removed]] table; [] without one."""
    if waivers is None:
        return []
    try:
        entries = tomllib.loads(Path(waivers).read_text(encoding="utf-8")).get("removed", [])
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"verifier_ratchet: {waivers}: {error}")
    for entry in entries:
        if not all(entry.get(k) for k in ("kind", "name", "reason")):
            raise SystemExit(f"verifier_ratchet: every [[removed]] entry in {waivers} needs kind, name and reason")
    return [(e["kind"], e["name"]) for e in entries]


def dropped(base, head, waivers=None):
    """What BASE has and HEAD lacks, less what the waivers file names. A name
    there may be a glob, so a deleted test file is one entry, not one per test."""
    before, after, ok = inventory(base), inventory(head), announced(waivers)
    return sorted((kind, name) for kind in before for name in before[kind] - after[kind]
                  if not any(kind == k and fnmatch.fnmatchcase(name, pattern) for k, pattern in ok))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head", nargs="?", default="HEAD")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--waivers", type=Path, help="the organization's waivers file")
    args = parser.parse_args(argv)
    missing = dropped(Repo(args.repo, args.base), Repo(args.repo, args.head), args.waivers)
    for kind, name in missing:
        print(f"verifier_ratchet: {kind} removed without a waiver: {name}")
    counts = {k: len(v) for k, v in inventory(Repo(args.repo, args.head)).items()}
    print(f"verifier_ratchet: {'REFUSED' if missing else 'ok'}; at {args.head}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
