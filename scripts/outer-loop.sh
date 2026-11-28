#!/usr/bin/env bash
# The outer loop. The inner loop (scripts/check.sh and Kani) goes round on every
# push in seconds. This runs what takes hours, and hands what it finds back to
# the inner loop as cases to rerun.
#
#   eval     Every AgentDojo case through the gate (no model is called),
#            compared with the last committed run by tools/eval_diff.py, and
#            every kernel step those cases took, as differential cases.
#            Root, on a disposable machine.
#   replay   Kernel steps through the Bend and wasm kernels: the eval's, and
#            those of any live run named on the command line, such as one
#            shadowed on vac-lab (its kernel.jsonl). The first case of each
#            fingerprint goes to new-cases.jsonl.
#   report   report.md, one line per fingerprint. Exits 1 if any is new.
#
# A new case goes into formal/cases/ in the pull request that fixes it. From
# then on scripts/check.sh reruns it on every push.
#
# usage: scripts/outer-loop.sh eval | replay [KERNEL_JSONL...] | report
#   OUTER_OUT       where everything goes (default target/outer)
#   OUTER_BASELINE  the committed run to compare with
#   OUTER_EVAL_ARGS which cases (default --all; "--cases 2" for a quick look)
#   OUTER_WAIVERS   the organization's waivers file
#                   (waivers/verified-agent-containment.toml in its .github
#                   repository); without it every AgentDojo group is new
set -euo pipefail
cd "$(dirname "$0")/.."

# A GitHub runner has python3 and no uv; the check image has uv and no python3.
py() { if command -v python3 >/dev/null 2>&1; then python3 "$@"; else uv run python "$@"; fi; }

out="${OUTER_OUT:-target/outer}"
baseline="${OUTER_BASELINE:-evals/agentdojo/runs/2026-11-25-domain}"
mkdir -p "$out"

case "${1:-}" in
  eval)
    [ "$(id -u)" = 0 ] || { echo "outer-loop: the eval runs sandboxes as root" >&2; exit 2; }
    [ "${FACTORY_LAB_DISPOSABLE:-}" = 1 ] || { echo "outer-loop: the eval runs attack code as root, so only on a disposable machine: set FACTORY_LAB_DISPOSABLE=1 to confirm" >&2; exit 1; }
    read -r -a pick <<< "${OUTER_EVAL_ARGS:---all}"
    rm -rf "$out/agentdojo" "$out/baseline"
    run_dir=/run/verified-agent-containment
    if [[ ",$(findmnt -no OPTIONS --target /run 2>/dev/null)," == *",noexec,"* ]] && ! mountpoint -q "$run_dir"; then
      mkdir -p "$run_dir"
      mount -t tmpfs -o mode=0755,nosuid,nodev,exec tmpfs "$run_dir"
    fi
    cargo build -q --locked -p containment-host
    evals/agentdojo/build_root.sh
    suites=(--suite workspace --suite travel --suite banking --suite slack)
    uv run evals/agentdojo/run.py "${suites[@]}" "${pick[@]}" --out "$out/agentdojo"
    uv run evals/agentdojo/run.py "${suites[@]}" "${pick[@]}" --benign --out "$out/agentdojo"
    before="$baseline"
    if [ "${pick[*]}" != "--all" ]; then
      # A partial run is compared with the same cases of the baseline only.
      before="$out/baseline"
      mkdir -p "$before"
      py -I - "$baseline/cases.jsonl" "$out/agentdojo/cases.jsonl" "$before/cases.jsonl" <<'PY'
import json, sys
ran = {json.loads(line)["case"] for line in open(sys.argv[2]) if line.strip()}
with open(sys.argv[3], "w") as kept:
    kept.writelines(line for line in open(sys.argv[1]) if line.strip() and json.loads(line)["case"] in ran)
PY
    fi
    waivers=()
    if [ -n "${OUTER_WAIVERS:-}" ]; then waivers=(--waivers "$OUTER_WAIVERS")
    else echo "outer-loop: OUTER_WAIVERS is not set, so no AgentDojo difference is waived" >&2; fi
    uv run tools/eval_diff.py "$before" "$out/agentdojo" ${waivers[@]+"${waivers[@]}"} \
      --json "$out/eval-diff.json" > "$out/eval-diff.txt" || [ $? = 1 ]
    : > "$out/traces.jsonl"
    find "$out/agentdojo" -path '*/live/kernel.jsonl' -print0 | sort -z | while IFS= read -r -d '' trace; do
      target/debug/containment-lab kernel-cases "$trace" >> "$out/traces.jsonl"
    done
    echo "outer-loop: $(wc -l < "$out/traces.jsonl") kernel cases from $(find "$out/agentdojo" -path '*/live/kernel.jsonl' | wc -l) runs"
    ;;
  replay)
    shift
    cargo build -q --locked -p containment-host
    cases="$out/replay.jsonl"
    if [ -f "$out/traces.jsonl" ]; then cp "$out/traces.jsonl" "$cases"; else : > "$cases"; fi
    for trace in "$@"; do target/debug/containment-lab kernel-cases "$trace" >> "$cases"; done
    [ -s "$cases" ] || { echo "outer-loop: no kernel steps to replay" >&2; exit 2; }
    mkdir -p target/generated
    bend formal/kernel.bend -o target/generated/kernel.mjs >/dev/null
    scripts/build-wasm.sh
    bun tools/differential.mjs target/generated/kernel.mjs "$cases" "$out/new-bend.jsonl" \
      > "$out/replay-bend.json" 2> "$out/replay-bend.groups" || [ $? = 1 ]
    node tools/wasm_check.mjs target/wasm32-unknown-unknown/release/containment_wasm.wasm "$cases" "$out/new-wasm.jsonl" \
      > "$out/replay-wasm.json" 2> "$out/replay-wasm.groups" || [ $? = 1 ]
    for side in bend wasm; do
      # A replay that crashed exits 1 too, but prints no summary.
      [ -s "$out/replay-$side.json" ] || { cat "$out/replay-$side.groups" >&2; echo "outer-loop: the $side replay did not finish" >&2; exit 2; }
    done
    sort -u "$out/new-bend.jsonl" "$out/new-wasm.jsonl" > "$out/new-cases.jsonl"
    cat "$out/replay-bend.json" "$out/replay-wasm.json"
    ;;
  report)
    status=0
    {
      echo "# Outer loop at $(git rev-parse --short HEAD 2>/dev/null || echo 'an unknown commit')"
      if [ -f "$out/eval-diff.json" ]; then
        new=$(py -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["unexpected_groups"])' "$out/eval-diff.json")
        [ "$new" = 0 ] || status=1
        printf '\n## AgentDojo compared with %s: %s new\n\n```\n' "${baseline##*/}" "$new"
        cat "$out/eval-diff.txt"
        printf '```\n'
      fi
      for side in bend wasm; do
        [ -f "$out/replay-$side.json" ] || continue
        printf '\n## Kernel steps replayed through %s\n\n```\n' "$side"
        cat "$out/replay-$side.json" "$out/replay-$side.groups"
        printf '```\n'
      done
      if [ -s "$out/new-cases.jsonl" ]; then
        status=1
        printf '\n%s new kernel case(s) in new-cases.jsonl, one per fingerprint. Each goes into formal/cases/ in the pull request that fixes it.\n' \
          "$(wc -l < "$out/new-cases.jsonl")"
      fi
      [ "$status" = 1 ] || printf '\nNothing new.\n'
    } > "$out/report.md"
    cat "$out/report.md"
    exit "$status"
    ;;
  *)
    sed -n '/^# usage/,/^# *OUTER_EVAL_ARGS/p' "$0" >&2
    exit 2
    ;;
esac
