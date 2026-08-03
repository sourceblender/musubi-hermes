#!/usr/bin/env bash
# Every gate, in ONE command, re-runnable by any reviewer.
#
# Yua: "no Musubi tests/evidence are committed... only temp DB remnants and commit
# prose." Evidence a reviewer cannot re-run is not evidence.
#
# And then, third pass: "run-all runs every suite TWICE (first piped to grep, second
# for status), duplicating live Musubi writes... ABC block prints mismatch but never
# exits nonzero." Both true. A test harness that runs your suite twice is writing twice
# to a real memory plane, and a check that cannot fail is decoration.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0

for t in tests/test_write_path.py tests/test_yua_gates.py \
         tests/test_upgrade_and_recovery.py tests/test_lease_contention.py \
         tests/test_ret003_pass_through.py \
         tests/test_ret007_warnings_passthrough.py; do
  echo "════════ $t"
  out="$(python3 "$t" 2>&1)"; status=$?      # RUN ONCE. Capture. Preserve the status.
  # The unittest suites report "Ran N tests" + OK/FAILED on stderr and never print
  # the word PASS. Greping for PASS alone rendered them as an EMPTY section that
  # looked identical to a suite producing nothing — 13 real RET-003 assertions were
  # invisible here. Match both dialects. (Aoi, 2026-08-03)
  echo "$out" | grep -E 'PASS|FAIL|RESULT|^Ran [0-9]+ test|^OK$' || true
  [ "$status" -ne 0 ] && fail=1
  echo
done

# A suite this harness cannot execute is not coverage, and must not be silent.
# tests/test_adapt001_cli_wrapper.py is 1334 lines of ADAPT-001 coverage that was
# never listed here AND imports pytest, which is on no venv or requirements file in
# this repo — so it has not run since it landed (2026-07-15, PR #6) and nothing said
# so. run-all's own opening line is "evidence a reviewer cannot re-run is not
# evidence." This block runs it when pytest is available and FAILS LOUDLY when it is
# not, rather than letting "ALL GATES PASSED" cover a suite that never executed.
echo "════════ tests/test_adapt001_cli_wrapper.py (requires pytest)"
if python3 -c "import pytest" 2>/dev/null; then
  out="$(python3 -m pytest -q tests/test_adapt001_cli_wrapper.py 2>&1)"; status=$?
  echo "$out" | tail -3
  [ "$status" -ne 0 ] && fail=1
elif command -v uv >/dev/null 2>&1; then
  # No global pytest, but uv can supply it without polluting the host or this repo.
  out="$(uv run --with pytest python -m pytest -q tests/test_adapt001_cli_wrapper.py 2>&1)"
  status=$?
  echo "$out" | tail -3
  [ "$status" -ne 0 ] && fail=1
else
  echo "  *** NOT RUN *** pytest unavailable and uv not installed — this suite is NOT gating."
  echo "      Fix: pip install pytest, or install uv   (then re-run this script)"
  fail=1
fi
echo

echo "════════ ABC conformance vs the real Hermes MemoryProvider"
python3 - <<'PY'
import sys, inspect, pathlib
sys.path.insert(0, str(pathlib.Path.home() / "Projects/hermes-agent"))
sys.path.insert(0, str(pathlib.Path.cwd()))
from agent.memory_provider import MemoryProvider
from musubi import MusubiMemoryProvider
ok = True
for m in ("sync_turn", "on_memory_write", "on_session_switch", "get_tool_schemas"):
    a = str(inspect.signature(getattr(MemoryProvider, m)))
    b = str(inspect.signature(getattr(MusubiMemoryProvider, m)))
    good = a == b
    ok &= good
    print(f"  {'PASS' if good else '*** FAIL ***'}  {m}")
    if not good:
        print(f"      abc : {a}")
        print(f"      ours: {b}")
print(f"  RESULT: {'4/4' if ok else 'MISMATCH'} signatures match the real ABC")
sys.exit(0 if ok else 1)        # a check that cannot FAIL is decoration
PY
[ $? -ne 0 ] && fail=1

echo
if [ "$fail" -eq 0 ]; then echo "ALL GATES PASSED"; else echo "*** GATES FAILED ***"; fi
exit $fail
