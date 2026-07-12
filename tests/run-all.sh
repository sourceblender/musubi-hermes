#!/usr/bin/env bash
# Every gate, in one command, re-runnable by any reviewer.
# Yua: "no Musubi tests/evidence are committed... only temp DB remnants and commit prose."
# Evidence a reviewer cannot re-run is not evidence. This is the answer to that.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_write_path.py tests/test_yua_gates.py \
         tests/test_upgrade_and_recovery.py tests/test_lease_contention.py; do
  echo "════════ $t"
  python3 "$t" 2>&1 | grep -E 'PASS|FAIL|RESULT' || true
  python3 "$t" >/dev/null 2>&1 || fail=1
  echo
done
echo "════════ ABC conformance vs the real Hermes MemoryProvider"
python3 - <<'PY'
import sys, inspect, pathlib
sys.path.insert(0, str(pathlib.Path.home()/"Projects/hermes-agent"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent if False else pathlib.Path.cwd()))
from agent.memory_provider import MemoryProvider
from musubi import MusubiMemoryProvider
ok = True
for m in ("sync_turn", "on_memory_write", "on_session_switch", "get_tool_schemas"):
    a = str(inspect.signature(getattr(MemoryProvider, m)))
    b = str(inspect.signature(getattr(MusubiMemoryProvider, m)))
    good = a == b
    ok &= good
    print(f"  {'PASS' if good else '*** FAIL ***'}  {m}")
print(f"  RESULT: {'4/4' if ok else 'MISMATCH'} signatures match the real ABC")
PY
exit $fail
