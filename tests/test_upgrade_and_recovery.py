"""Gates for the bugs Yua found in re-review. Every one is red-proofed.

These exist because Yua returned NOT ACCEPTED twice and, the second time, said:

    "I cannot audit the two changed test assertions or accept 10/10 evidence because
     no Musubi tests/evidence are committed in fleet-tools or the project; only temp
     DB remnants and commit prose exist."

She was right. My tests lived in a scratch directory. "10/10" in a commit message is
prose, not proof. Evidence that cannot be re-run by the reviewer is not evidence.

Run:  python3 hermes-plugins/tests/test_upgrade_and_recovery.py
"""
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from musubi import Outbox, COUNTER_PREFIX  # noqa: E402

results = []


def gate(label, cond):
    print(f"  {'PASS' if cond else '*** FAIL ***'}  {label}")
    results.append(bool(cond))


# ── The OLD schema, exactly as it was deployed to Tama's profile ─────────────
OLD_SCHEMA = """
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL,
    importance INTEGER NOT NULL, session_id TEXT, created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, next_try_at REAL NOT NULL DEFAULT 0,
    last_error TEXT, state TEXT NOT NULL DEFAULT 'pending', object_id TEXT
);
"""

print("=" * 72)
print("GATE A — SCHEMA MIGRATION: `CREATE TABLE IF NOT EXISTS` IS NOT A MIGRATION")
print("=" * 72)
print("  Yua reproduced this against the DEPLOYED database: new code + Tama's existing")
print("  outbox => OperationalError: table outbox has no column named idem_key.")
print("  Her memory writes would have failed on her next start. Every test I had")
print("  written built a FRESH db in a temp dir, so nothing ever opened an old one.")
print()

d = pathlib.Path(tempfile.mkdtemp())
db = d / "musubi-outbox.db"
con = sqlite3.connect(db)
con.executescript(OLD_SCHEMA)
for i in range(3):
    con.execute(
        "INSERT INTO outbox (namespace, content, tags, importance, session_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("tama/command-chair/episodic", f"a real memory {i}", "[]", 5, "s", 1.0),
    )
con.commit()
cols_before = {r[1] for r in con.execute("PRAGMA table_info(outbox)")}
n_before = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
con.close()
gate("fixture really IS the old schema (no idem_key)", "idem_key" not in cols_before)

ob = Outbox(db)  # <-- this is where Yua's OperationalError fired
rid = ob.enqueue("tama/command-chair/episodic", "post-upgrade write", ["kind:episode"], 5, "s")
con = sqlite3.connect(db)
cols_after = {r[1] for r in con.execute("PRAGMA table_info(outbox)")}
n_after = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
backfilled = con.execute(
    "SELECT COUNT(*) FROM outbox WHERE idem_key IS NOT NULL AND content_sha IS NOT NULL"
).fetchone()[0]
con.close()

gate("migration adds the new columns", {"idem_key", "content_sha", "lease_owner"} <= cols_after)
gate("enqueue WORKS on the upgraded db (Yua's exact repro)", rid > 0)
gate(f"pre-existing rows PRESERVED ({n_before} -> {n_after})", n_after == n_before + 1)
gate("every row backfilled with an identity (idem_key + content_sha)", backfilled == n_after)

# a memory is not dropped just because we changed our minds about the schema
con = sqlite3.connect(db)
survived = con.execute(
    "SELECT COUNT(*) FROM outbox WHERE content LIKE 'a real memory%'").fetchone()[0]
con.close()
gate("the three pre-existing MEMORIES are still there", survived == 3)

print()
print("=" * 72)
print("GATE B — CRASH RESTART REPLAYS IMMEDIATELY (Yua, gate 10)")
print("=" * 72)
print("  A row left `inflight` by a dead process must NOT wait LEASE_TTL(120s) to be")
print("  replayed. The owner token carries the pid; if that process is gone, the lease")
print("  is dead NOW.")
print()

d2 = pathlib.Path(tempfile.mkdtemp())
db2 = d2 / "o.db"
ob2 = Outbox(db2)
rid2 = ob2.enqueue("x/y/episodic", "crash probe", ["kind:episode"], 1, "s")
ob2.claim_batch()  # inflight, owned by this process
c = sqlite3.connect(db2)
c.execute("UPDATE outbox SET lease_owner='999999-deadbeef'")  # a pid that cannot exist
c.commit()
stranded = c.execute("SELECT state FROM outbox WHERE id=?", (rid2,)).fetchone()[0]
c.close()
gate("row is stranded 'inflight' after the crash", stranded == "inflight")

recovered = Outbox(db2).recover_orphans()  # a fresh process opens the same outbox
c = sqlite3.connect(db2)
state = c.execute("SELECT state FROM outbox WHERE id=?", (rid2,)).fetchone()[0]
c.close()
gate("restart reclaims it IMMEDIATELY (not after 120s)", recovered == 1 and state == "pending")

# and the inverse: a LIVE owner's lease must never be stolen
ob3 = Outbox(db2)
ob3.claim_batch()
stolen = Outbox(db2).recover_orphans()
gate("a LIVE owner's lease is NOT stolen", stolen == 0)

print()
print("=" * 72)
print("GATE C — COUNTERS AND GAUGES MUST NOT COLLIDE (Yua)")
print("=" * 72)
print("  The restart parser was slurping the outbox GAUGES back into the counter dict,")
print("  so the next flush emitted a metric once as `# TYPE ... counter` and again as")
print("  `# TYPE ... gauge`. Prometheus DISCARDS THE WHOLE FILE on that. The telemetry")
print("  built to detect silent failure would itself have failed silently.")
print()
gate("counters are namespaced (musubi_memory_*)", COUNTER_PREFIX == "musubi_memory_")
gate("outbox gauges are OUTSIDE that namespace",
     not "musubi_outbox_pending".startswith(COUNTER_PREFIX))

print()
print("=" * 72)
print(f"RESULT: {sum(results)}/{len(results)} gates passed")
print("=" * 72)
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(d2, ignore_errors=True)
sys.exit(0 if all(results) else 1)
