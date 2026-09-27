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

shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(d2, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# GATE D — TELEMETRY RESTART PRODUCES A VALID SCRAPE (Yua, third-pass audit)
#
# She was right that Gate C was weak: it only checked PREFIX CONSTANTS. That proves
# nothing about what the file actually contains after a restart. Reproduce it for real:
# emit, flush, RESTART (reload from the file on disk), emit again, flush again — then
# parse the file and assert NO metric is declared with two different TYPEs.
#
# The original bug: the restart parser slurped the outbox GAUGES back into the counter
# dict, so the next flush emitted e.g. musubi_outbox_pending under `# TYPE ... counter`
# AND `# TYPE ... gauge`. Prometheus DISCARDS THE ENTIRE FILE on that. The telemetry
# built to detect silent failure would itself have failed silently.
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("GATE D — a RESTART must still produce a VALID Prometheus scrape")
print("=" * 72)

import tempfile as _tf, pathlib as _pl, os as _os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from musubi import MusubiMemoryProvider  # noqa: E402

_home = _pl.Path(_tf.mkdtemp())
_env = _home / "musubi.env"
_env.write_text("MUSUBI_API_URL=http://127.0.0.1:1/v1\nMUSUBI_TOKEN=test-token\n")
(_home / "config.yaml").write_text(
    f"musubi:\n  tenant: aoi\n  presence: command-chair\n  env_file: {_env}\n")
_os.environ["HERMES_HOME"] = str(_home)


def _types_in(path):
    """metric -> set of TYPEs declared for it. A valid scrape has exactly one each."""
    types = {}
    for line in path.read_text().splitlines():
        if line.startswith("# TYPE "):
            _, _, metric, typ = line.split()
            types.setdefault(metric, set()).add(typ)
    return types


p1 = MusubiMemoryProvider()
p1.initialize("s1", hermes_home=str(_home), platform="cli", agent_context="primary")
p1._emit_metric("musubi_memory_writes_total", 1, status="success")
p1._flush_metrics()
mf = p1._metrics_file
gate("metrics file written", mf.exists())

# ---- A REAL RESTART. Yua: "p2 is initialized before p1.shutdown, so it is NOT a
# restart; it creates two live providers/workers sharing DB+metrics file and can
# introduce races unrelated to restart." She is right — my test proved nothing about
# restarting. Shut the first one ALL the way down, THEN construct the second.
p1.shutdown()
del p1

p2 = MusubiMemoryProvider()
p2.initialize("s2", hermes_home=str(_home), platform="cli", agent_context="primary")
p2._emit_metric("musubi_memory_writes_total", 1, status="success")
p2._flush_metrics()

types = _types_in(mf)
dupes = {m: t for m, t in types.items() if len(t) > 1}
gate(f"NO metric declared with two TYPEs after restart (dupes={dupes})", not dupes)

# SAMPLE-level uniqueness too (Yua): the same series must not appear twice in one file.
samples = [l.split(" ")[0] for l in mf.read_text().splitlines()
           if l and not l.startswith("#")]
gate(f"no DUPLICATE SAMPLE lines ({len(samples)} samples, {len(set(samples))} unique)",
     len(samples) == len(set(samples)))

# and if promtool is on the box, let the real parser judge it
import shutil as _sh, subprocess as _sp
if _sh.which("promtool"):
    r = _sp.run(["promtool", "check", "metrics"], stdin=mf.open(), capture_output=True)
    gate(f"promtool accepts the scrape ({r.stderr.decode()[:60]})", r.returncode == 0)
else:
    print("  (skip) promtool not installed — TYPE + sample uniqueness asserted instead")
gate("the outbox gauges are declared as gauge, exactly once",
     types.get("musubi_outbox_pending") == {"gauge"})
gate("the write counter is declared as counter, exactly once",
     types.get("musubi_memory_writes_total") == {"counter"})

# counters must be MONOTONIC across the restart — Shiori's rate() depends on it
vals = [int(l.rsplit(" ", 1)[1]) for l in mf.read_text().splitlines()
        if l.startswith("musubi_memory_writes_total{")]
gate(f"counter survived the restart and INCREMENTED (got {vals})", vals and max(vals) >= 2)

p2.shutdown()
shutil.rmtree(_home, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# GATE E — PARTIAL MIGRATION RECOVERY (Yua, fourth pass)
#
#   "If a prior migration dies after ALTER columns but before/during backfill, next
#    start sees columns present, `added` empty, skips NULL repair forever, then creates
#    UNIQUE index that permits multiple NULLs."
#
# She is right, and SQLite really does permit multiple NULLs in a UNIQUE index — so the
# index would not even complain. Rows would sit permanently with no idempotency key and
# replay as DUPLICATE memories on every retry, silently.
#
# Fixture: columns EXIST, some rows already backfilled, some rows NULL. That is exactly
# the state a crashed migration leaves behind.
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("GATE E — a HALF-FINISHED migration must repair itself on the next open")
print("=" * 72)

d3 = pathlib.Path(tempfile.mkdtemp())
db3 = d3 / "half.db"
c = sqlite3.connect(db3)
c.executescript(OLD_SCHEMA)
# the ALTERs succeeded...
for col, ddl in (("idem_key", "TEXT"), ("content_sha", "TEXT"), ("leased_at", "REAL"),
                 ("lease_owner", "TEXT"), ("consec_fail", "INTEGER NOT NULL DEFAULT 0"),
                 ("verified_at", "REAL")):
    c.execute(f"ALTER TABLE outbox ADD COLUMN {col} {ddl}")
# ...and then the process DIED mid-backfill: row 1 got its identity, rows 2 and 3 did not
for i in range(3):
    c.execute("INSERT INTO outbox (namespace, content, tags, importance, session_id, "
              "created_at) VALUES (?,?,?,?,?,?)",
              ("t/c/episodic", f"half-migrated memory {i}", "[]", 5, "s", 1.0))
c.execute("UPDATE outbox SET idem_key='PRE-EXISTING-KEY-DO-NOT-TOUCH', "
          "content_sha='PRE-EXISTING-SHA' WHERE id=1")
c.commit()
nulls_before = c.execute(
    "SELECT COUNT(*) FROM outbox WHERE idem_key IS NULL OR content_sha IS NULL").fetchone()[0]
c.close()
gate(f"fixture is a HALF-MIGRATED db (columns present, {nulls_before} rows NULL)",
     nulls_before == 2)

Outbox(db3)   # the next start — must repair, though `added` is EMPTY

c = sqlite3.connect(db3)
nulls_after = c.execute(
    "SELECT COUNT(*) FROM outbox WHERE idem_key IS NULL OR content_sha IS NULL").fetchone()[0]
kept = c.execute("SELECT idem_key, content_sha FROM outbox WHERE id=1").fetchone()
keys = [r[0] for r in c.execute("SELECT idem_key FROM outbox")]
c.close()

gate("the NULL rows were REPAIRED even though no column was added", nulls_after == 0)
gate("a row that ALREADY had an identity was NOT overwritten",
     kept == ("PRE-EXISTING-KEY-DO-NOT-TOUCH", "PRE-EXISTING-SHA"))
gate("every idem_key is unique (no two rows share one)", len(keys) == len(set(keys)))

# and it must be safely repeatable — opening again changes nothing
Outbox(db3)
c = sqlite3.connect(db3)
keys2 = [r[0] for r in c.execute("SELECT idem_key FROM outbox ORDER BY id")]
c.close()
gate("re-opening is IDEMPOTENT — no key churn", keys2 == sorted(keys, key=lambda k: keys.index(k)) or set(keys2) == set(keys))
shutil.rmtree(d3, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# GATE F — the lease owner must be PER-INSTANCE, not import-time (Yua)
# A class attribute is evaluated once at import; os.fork() copies it verbatim, so
# parent and child would present the SAME owner and each would honour the other's
# leases — the duplicate-delivery race, reintroduced by where I put one line.
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("GATE F — two Outbox instances must NOT share an owner token")
print("=" * 72)
d4 = pathlib.Path(tempfile.mkdtemp())
o1, o2 = Outbox(d4 / "x.db"), Outbox(d4 / "x.db")
gate("two instances mint DIFFERENT owner tokens", o1.owner != o2.owner)
gate("the token is an instance attribute, not a class constant",
     "owner" in vars(o1) and not hasattr(Outbox, "OWNER"))
shutil.rmtree(d4, ignore_errors=True)

print()
print("=" * 72)
print(f"FINAL: {sum(results)}/{len(results)} gates passed")
print("=" * 72)
sys.exit(0 if all(results) else 1)
