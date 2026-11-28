#!/usr/bin/env python3
"""Compare two AgentDojo runs case by case and group the differences by fingerprint.

Each case is checked in the order the pipeline produces it: did the program
start, what the gate said to each request, which mode the controller ended in,
the runner's outcome, and AgentDojo's own verdict. The first stage that differs
is the case's fingerprint, together with the values on both sides. One cause
tends to leave the same fingerprint on every case it touches, so the groups,
not the cases, are what to look at.

A difference that is meant (a policy made finer on purpose, say) is listed,
with the reason, under [[expected]] in the waivers file, and does not fail the
comparison. Every other group does. The waivers file is
waivers/verified-agent-containment.toml in the organization's .github
repository, which agents do not edit; nothing in this repository is read for
waivers. Without --waivers, every group is new.

usage: uv run tools/eval_diff.py BEFORE_DIR AFTER_DIR [--waivers FILE] [--json OUT]

Exit 0 when every group is expected, 1 when any is not, 2 on bad input.
"""
import argparse
import fnmatch
import json
import sys
import tomllib
from pathlib import Path

EXAMPLES = 3


def load(run_dir):
    path = Path(run_dir) / "cases.jsonl"
    if not path.is_file():
        raise SystemExit(f"eval_diff: {path} not found")
    cases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            cases[record["case"]] = record  # a rerun of a case replaces the earlier line
    return cases


def _gate(request):
    if request.get("gate") == "refused":
        return f"refused ({request.get('gate_error')})"
    return str(request.get("gate"))


def stages(record):
    """The case's comparable results, in pipeline order: (stage, value)."""
    yield "program started", bool((record.get("program") or {}).get("started"))
    requests = {r["rid"]: r for r in record.get("requests", [])}
    yield "request count", len(requests)
    for rid in sorted(requests):
        r = requests[rid]
        yield f"gate on {r.get('phase')} {r.get('tool')}", _gate(r)
    yield "mode changes", [f"{m['from']}>{m['to']} on {m['event']}" for m in record.get("mode_changes", [])]
    yield "final mode", record.get("final_mode")
    yield "outcome", record.get("outcome")
    if record.get("outcome") == "error":
        yield "error reason", record.get("reason")
    yield "agentdojo injection_succeeded", (record.get("agentdojo") or {}).get("injection_succeeded")


def fingerprint(before, after):
    """(stage, before value, after value) at the first stage that differs, or None."""
    a, b = list(stages(before)), list(stages(after))
    for (stage, x), (other, y) in zip(a, b):
        if stage != other:
            return "request sequence", stage, other
        if x != y:
            return stage, x, y
    return None


def expected_rules(path):
    if path is None:
        return []
    try:
        rules = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("expected", [])
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"eval_diff: {path}: {error}")
    for rule in rules:
        missing = [k for k in ("stage", "before", "after", "reason") if k not in rule]
        if missing:
            raise SystemExit(f"eval_diff: an [[expected]] entry in {path} has no {', '.join(missing)}")
    return rules


def matches(rule, suite, stage, before, after):
    return (fnmatch.fnmatchcase(suite, rule.get("suite", "*"))
            and fnmatch.fnmatchcase(stage, rule["stage"])
            and fnmatch.fnmatchcase(json.dumps(before), rule["before"])
            and fnmatch.fnmatchcase(json.dumps(after), rule["after"]))


def compare(before, after, rules):
    groups = {}
    for case in sorted(before.keys() | after.keys()):
        suite = case.split(".", 1)[0]
        if case not in after:
            key = (suite, "case present", "true", "false")
        elif case not in before:
            key = (suite, "case present", "false", "true")
        else:
            fp = fingerprint(before[case], after[case])
            if fp is None:
                continue
            stage, x, y = fp
            key = (suite, stage, json.dumps(x), json.dumps(y))
        groups.setdefault(key, []).append(case)
    out = []
    for (suite, stage, x, y), cases in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        x, y = json.loads(x), json.loads(y)
        rule = next((r for r in rules if matches(r, suite, stage, x, y)), None)
        out.append({
            "suite": suite, "stage": stage, "before": x, "after": y, "cases": len(cases),
            "examples": cases[:EXAMPLES], "expected": rule["reason"] if rule else None,
        })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--waivers", "--expected", dest="waivers", type=Path, help="the organization's waivers file")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args(argv)

    before, after = load(args.before), load(args.after)
    groups = compare(before, after, expected_rules(args.waivers))
    unexpected = [g for g in groups if g["expected"] is None]
    report = {
        "before": str(args.before), "after": str(args.after),
        "cases": len(before.keys() | after.keys()),
        "cases_differing": sum(g["cases"] for g in groups),
        "groups": len(groups), "unexpected_groups": len(unexpected),
        "fingerprints": groups,
    }
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for g in groups:
        mark = "expected" if g["expected"] else "NEW"
        print(f"{mark:>8} {g['cases']:>4}  {g['suite']}: {g['stage']}: {json.dumps(g['before'])} -> {json.dumps(g['after'])}")
        print(f"{'':>14}rerun one: uv run evals/agentdojo/run.py --suite {g['suite']} --case {g['examples'][0]} --out OUT")
    print(json.dumps({k: report[k] for k in ("cases", "cases_differing", "groups", "unexpected_groups")}))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
