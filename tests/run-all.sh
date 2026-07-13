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
         tests/test_ret003_pass_through.py; do
  echo "════════ $t"
  out="$(python3 "$t" 2>&1)"; status=$?      # RUN ONCE. Capture. Preserve the status.
  echo "$out" | grep -E 'PASS|FAIL|RESULT' || true
  [ "$status" -ne 0 ] && fail=1
  echo
done

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
