"""Red-proof Yua's P0/P1 findings. Each gate must be SEEN failing on the old behaviour."""
import os, sys, time, tempfile, pathlib, threading, sqlite3, hashlib, subprocess
sys.path.insert(0, str(pathlib.Path.home() / "Vaults/fleet-tools/hermes-plugins"))
import musubi
from musubi import MusubiMemoryProvider, MusubiError

ENV = str(pathlib.Path.home() / ".musubi/musubi-mcp-aoi.env")
res = []

def ok(label, cond):
    print(f"  {'PASS' if cond else '*** FAIL ***'}  {label}")
    res.append(cond)

def prov(context="primary", tenant="aoi", presence="command-chair", env=ENV):
    home = tempfile.mkdtemp(prefix="yr-")
    pathlib.Path(home, "config.yaml").write_text(
        f"musubi:\n  tenant: {tenant}\n  presence: {presence}\n  env_file: {env}\n")
    os.environ["HERMES_HOME"] = home
    p = MusubiMemoryProvider()
    p.initialize("yr", hermes_home=home, platform="discord", agent_context=context)
    return p, home

print("P0-1  IDEMPOTENCY — a replayed row must NOT duplicate the memory")
p, home = prov()
rid = p._enqueue("idempotency red-proof: replay must not duplicate", plane="lifecycle")
row = p._outbox.row(rid)
ok("row carries a persisted idempotency key", bool(row["idem_key"]))
# deliver twice with the SAME key, exactly as a crash-then-retry would
id1 = p._client.write(row["namespace"], row["content"], ["kind:episode","staleness:episodic"], 1,
                      idempotency_key=row["idem_key"])
id2 = p._client.write(row["namespace"], row["content"], ["kind:episode","staleness:episodic"], 1,
                      idempotency_key=row["idem_key"])
ok(f"replay returns the SAME object_id (no duplicate memory)", id1 == id2)
p.shutdown()

print()
print("P0-1b ATOMIC LEASE — two concurrent drains must not claim the same row")
p, home = prov()
p._stop.set()  # silence the real worker; we drive claims by hand
for i in range(5):
    p._enqueue(f"lease probe {i}", plane="lifecycle")
seen, dupes, lock = [], [], threading.Lock()
def claim():
    b = p._outbox.claim_batch(limit=5)
    with lock:
        for r in b:
            if r["id"] in seen: dupes.append(r["id"])
            seen.append(r["id"])
ts = [threading.Thread(target=claim) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
ok(f"no row claimed twice across 4 concurrent drains (dupes={dupes})", not dupes)
p.shutdown()

print()
print("P0-2  READBACK IDENTITY — a wrong object must NOT be marked verified")
p, home = prov()
rid = p._enqueue("identity red-proof", plane="lifecycle")
real_read_back = p._client.read_back
# INJECT THE FAULT: Musubi returns a DIFFERENT object. Old code marked this verified.
p._client.read_back = lambda ns, oid: {"object_id": "WRONG-OBJECT", "namespace": ns,
                                       "content": "some other memory entirely"}
p._drain_once()
r = p._outbox.row(rid)
ok(f"wrong object => NOT verified (state={r['state']})", r["state"] != "verified")
ok("marked dead with an identity-mismatch reason",
   r["state"] == "dead" and "IDENTITY MISMATCH" in (r["last_error"] or ""))
p._client.read_back = real_read_back
p.shutdown()

print()
print("P0-3  PROPAGATION — a durability failure must RAISE, not be swallowed")
print("      (Yua, third-pass audit: THIS GATE WAS VACUOUS. It called")
print("       sync_turn(user_message=..., assistant_message=...) — the OLD, INVALID")
print("       signature — so a TypeError fired BEFORE the injected fault was ever")
print("       reached, and `except Exception: raised=True` counted that as a PASS.")
print("       The gate proved nothing and I quoted it as evidence.)")
p, home = prov()

called = {"n": 0}
def boom(*a, **k):
    called["n"] += 1
    raise sqlite3.OperationalError("database or disk is full")
p._outbox.enqueue = boom

err = None
try:
    p.sync_turn("u", "a", session_id="s")     # the REAL signature
except Exception as e:
    err = e

ok("the injected fault was ACTUALLY REACHED (not short-circuited by a TypeError)",
   called["n"] == 1)
ok("sync_turn RAISES rather than swallowing", err is not None)
ok(f"it is NOT a TypeError from a bad call ({type(err).__name__})",
   err is not None and not isinstance(err, TypeError))
ok("the failure that surfaces is the DISK failure",
   err is not None and "disk is full" in str(err).lower())
p.shutdown()

print()
print("P1    CONTEXT FAILS CLOSED — a MISSING context must not gain write authority")
home = tempfile.mkdtemp(prefix="yr-")
pathlib.Path(home, "config.yaml").write_text(
    f"musubi:\n  tenant: aoi\n  presence: command-chair\n  env_file: {ENV}\n")
os.environ["HERMES_HOME"] = home
p = MusubiMemoryProvider()
p.initialize("yr", hermes_home=home, platform="cli")   # NOTE: no agent_context at all
ok(f"missing context resolves to '{p._context}', NOT primary", p._context != "primary")
ok("missing context is NOT writable", p._writable() is False)
ok("missing context starts NO drain worker", p._worker is None)
p.shutdown()

print()
print("P1    NON-PRIMARY starts no worker (zero-network contract)")
p, home = prov(context="cron")
ok("cron starts no drain worker", p._worker is None)
p.shutdown()


print("P0-4  ACCEPTED-BEFORE-READBACK — a verification retry must NEVER re-POST")
print("      (Yua, long-term audit: 'Musubi idempotency expires after 24h, so a row")
print("       unverified longer than 24h can duplicate.' The key only protects a replay")
print("       while the server still remembers it. A row stuck 25 hours would POST again")
print("       with an EXPIRED key and create a SECOND memory.)")
p, home = prov()
posts = {"n": 0}
real_write = p._client.write
def counting_write(*a, **k):
    posts["n"] += 1
    return real_write(*a, **k)
p._client.write = counting_write
# make the readback fail so the row must be RETRIED
fail = {"on": True}
real_rb = p._client.read_back
def flaky_rb(ns, oid):
    if fail["on"]:
        raise MusubiError("transient readback failure", status=503, retryable=True)
    return real_rb(ns, oid)
p._client.read_back = flaky_rb

rid = p._enqueue("accepted-before-readback probe", plane="lifecycle")
p._drain_once()                                   # POST ok, readback fails
r = p._outbox.row(rid)
ok(f"row is ACCEPTED with an object_id after the POST (state={r['state']})",
   r["state"] == "accepted" and bool(r["object_id"]))
ok("exactly ONE post so far", posts["n"] == 1)

# force it to be retried immediately, several times
for _ in range(3):
    import sqlite3 as _s
    with p._outbox._connect() as c:
        c.execute("UPDATE outbox SET next_try_at=0 WHERE id=?", (rid,))
    p._drain_once()
ok(f"THREE retries later, still exactly ONE post (posts={posts['n']})", posts["n"] == 1)

fail["on"] = False
with p._outbox._connect() as c:
    c.execute("UPDATE outbox SET next_try_at=0 WHERE id=?", (rid,))
p._drain_once()
r = p._outbox.row(rid)
ok(f"once readback succeeds it VERIFIES (state={r['state']})", r["state"] == "verified")
ok(f"and it NEVER posted a second time (posts={posts['n']})", posts["n"] == 1)
p.shutdown()


print()
print("P0-5  EXACTLY-ONCE BEYOND THE SERVER'S IDEMPOTENCY TTL (Yua, residual hole)")
print("      'crash after server commits but before client persists object_id, followed")
print("       by downtime beyond the server 24h idempotency TTL' -> the key expires, we")
print("       re-POST, and a DUPLICATE MEMORY is born. The receipt tag rides WITH the")
print("       memory, so it outlives the window: a recovered row ASKS before it posts.")
p, home = prov()
# a row that was committed on the server but whose ack we lost, and whose idempotency
# key has since EXPIRED (simulated: the header no longer dedupes)
rid = p._enqueue("receipt-tag exactly-once probe", plane="episodic")
p._drain_once()                              # committed + verified normally
r = p._outbox.row(rid)
committed_id = r["object_id"]
ok("first write committed", bool(committed_id))

# now simulate the crash: wipe our knowledge of the object_id, force a retry, and make
# the server's Idempotency-Key USELESS (as it would be after 24h)
with p._outbox._connect() as c:
    c.execute("UPDATE outbox SET state='pending', object_id=NULL, attempts=1, next_try_at=0 "
              "WHERE id=?", (rid,))
posts = {"n": 0}
real_write = p._client.write
def counting_write(*a, **k):
    posts["n"] += 1
    return real_write(*a, **k)
p._client.write = counting_write

p._drain_once()
r = p._outbox.row(rid)
ok(f"the recovered row FOUND its own receipt instead of re-posting (posts={posts['n']})",
   posts["n"] == 0)
ok(f"and it adopted the ORIGINAL object_id (no duplicate memory)",
   r["object_id"] == committed_id)
p.shutdown()

print()
print("P1    BOUNDED RETENTION — receipts and dead rows must not grow forever")
p, home = prov()
import time as _t
with p._outbox._connect() as c:
    for i in range(3):
        c.execute("INSERT INTO outbox (idem_key, content_sha, namespace, content, tags, "
                  "importance, created_at, state, verified_at, object_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (f"old-{i}", "x", "a/b/episodic", None, "[]", 1, 1.0, "verified",
                   _t.time() - 8*24*3600, f"obj{i}"))
with p._outbox._connect() as c:
    for i in range(30):
        c.execute("INSERT INTO outbox (idem_key, content_sha, namespace, content, tags, "
                  "importance, created_at, state) VALUES (?,?,?,?,?,?,?,?)",
                  (f"dead-{i}", "x", "a/b/episodic", f"A FAILED MEMORY {i}", "[]", 1,
                   1.0 + i, "dead"))
pruned = p._outbox.prune()
ok(f"verified receipts past the retention window are pruned ({pruned['verified_pruned']})",
   pruned["verified_pruned"] == 3)
ok(f"DEAD rows are NEVER auto-deleted (dead_pruned={pruned['dead_pruned']})",
   pruned["dead_pruned"] == 0)
with p._outbox._connect() as c:
    still = c.execute("SELECT COUNT(*) FROM outbox WHERE state='dead'").fetchone()[0]
    kept = c.execute("SELECT COUNT(*) FROM outbox WHERE state='dead' AND content IS NOT NULL"
                     ).fetchone()[0]
ok(f"every failed memory is STILL THERE, with its content ({kept}/{still})",
   still == 30 and kept == 30)
ok("and it says so loudly instead of tidying up",
   pruned["dead_awaiting_operator"] == 30)
p.shutdown()

print()
print("P0-6  RECEIPT LOOKUP MUST FAIL CLOSED (Yua)")
print("      'a transient retrieve timeout/503 after the 24h TTL creates the duplicate")
print("       this feature exists to prevent.' COULD NOT CHECK != NOT THERE.")
p, home = prov()
rid = p._enqueue("fail-closed probe", plane="episodic")
with p._outbox._connect() as c:
    c.execute("UPDATE outbox SET attempts=1 WHERE id=?", (rid,))   # a RECOVERED row
posts = {"n": 0}
real_w = p._client.write
p._client.write = lambda *a, **k: (posts.__setitem__("n", posts["n"] + 1), real_w(*a, **k))[1]
# the receipt lookup is DOWN
p._client.find_by_receipt = lambda ns, k: (_ for _ in ()).throw(
    MusubiError("503 retrieve unavailable", status=503, retryable=True))
p._drain_once()
r = p._outbox.row(rid)
ok(f"a 503 on the receipt lookup causes ZERO posts (posts={posts['n']})", posts["n"] == 0)
ok(f"the row is DEFERRED, not lost (state={r['state']})", r["state"] in ("pending", "accepted"))
ok("and the reason says why", "DEFERRING" in (r["last_error"] or ""))
p.shutdown()

print()
print(f"RESULT: {sum(res)}/{len(res)} gates passed")
sys.exit(0 if all(res) else 1)
