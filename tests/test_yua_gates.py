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
p, home = prov()
def boom(*a, **k): raise sqlite3.OperationalError("disk is full")
p._outbox.enqueue = boom
raised = False
try:
    p.sync_turn(user_message="u", assistant_message="a")
except Exception:
    raised = True
ok("sync_turn RAISES when the outbox cannot durably accept the write", raised)
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

print()
print(f"RESULT: {sum(res)}/{len(res)} gates passed")
sys.exit(0 if all(res) else 1)
