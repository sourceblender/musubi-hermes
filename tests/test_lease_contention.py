"""TWO REAL PROCESSES fight over one outbox. Yua required this, not a thread test.

    "The lease has no owner token; a two-connection/process contention proof is
     required before calling the claim primitive atomic across processes."

Threads share a `threading.Lock`. Processes do not. A lock that only exists inside one
interpreter proves nothing about two Hermes instances on the same profile — which is
not hypothetical, because the whole point of a durable queue is that it outlives the
process that wrote to it.

If a row is claimed by both processes, it gets POSTed twice: a duplicated memory.

Run:  python3 hermes-plugins/tests/test_lease_contention.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from musubi import Outbox  # noqa: E402

CHILD = r'''
import sys, os, json, time, pathlib
sys.path.insert(0, {root!r})
from musubi import Outbox
ob = Outbox(pathlib.Path({db!r}))
# Claim in SMALL batches, in a loop, so both processes genuinely CONTEND for the same
# rows over and over. A single greedy claim lets the first process take everything and
# the race never actually happens — a test where the race never occurs proves nothing
# about the race.
claimed = []
deadline = time.time() + 5
while time.time() < deadline:
    batch = ob.claim_batch(limit=3)
    if not batch:
        break
    claimed.extend(r["id"] for r in batch)
    time.sleep(0.005)
print(json.dumps({{"pid": os.getpid(), "claimed": claimed}}))
'''

results = []


def gate(label, cond):
    print(f"  {'PASS' if cond else '*** FAIL ***'}  {label}")
    results.append(bool(cond))


d = pathlib.Path(tempfile.mkdtemp())
db = d / "musubi-outbox.db"

ob = Outbox(db)
N = 40
for i in range(N):
    ob.enqueue("x/y/episodic", f"memory {i}", ["kind:episode"], 1, "s")
print(f"  seeded {N} pending rows in one outbox\n")

# Two SEPARATE OS PROCESSES race for the same rows, at the same time.
procs = [
    subprocess.Popen(
        [sys.executable, "-c", CHILD.format(root=str(ROOT), db=str(db))],
        stdout=subprocess.PIPE, text=True,
    )
    for _ in range(2)
]
outs = [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in procs]

a, b = set(outs[0]["claimed"]), set(outs[1]["claimed"])
overlap = a & b
print(f"  process {outs[0]['pid']} claimed {len(a)} rows")
print(f"  process {outs[1]['pid']} claimed {len(b)} rows")
print(f"  OVERLAP: {len(overlap)} rows claimed by BOTH\n")

gate("no row was claimed by two processes (a duplicated memory)", not overlap)
gate("every row was claimed by exactly one process", len(a) + len(b) == N)
gate("both processes did real work (the test is not vacuous)", a and b)

# each surviving lease must name its owner, and they must differ
import sqlite3  # noqa: E402
con = sqlite3.connect(db)
owners = {r[0] for r in con.execute(
    "SELECT DISTINCT lease_owner FROM outbox WHERE state='inflight'")}
con.close()
gate("every leased row records an owner", None not in owners and owners)
gate("the two processes hold DIFFERENT owner tokens", len(owners) == 2)

print()
print(f"RESULT: {sum(results)}/{len(results)} gates passed")
shutil.rmtree(d, ignore_errors=True)
sys.exit(0 if all(results) else 1)
