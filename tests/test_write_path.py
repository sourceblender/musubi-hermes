"""Red-proof the Musubi provider. Every claim must be seen FAILING, not just passing."""
import os, sys, time, tempfile, pathlib, sqlite3, json

sys.path.insert(0, str(pathlib.Path.home() / "Vaults/fleet-tools/hermes-plugins"))
from musubi import MusubiMemoryProvider, Outbox, MusubiError  # noqa

HOME = tempfile.mkdtemp(prefix="hermes-rp-")
ENV = str(pathlib.Path.home() / ".musubi/musubi-mcp-aoi.env")

def write_cfg(tenant, presence, env_file):
    pathlib.Path(HOME, "config.yaml").write_text(
        f"musubi:\n  tenant: {tenant}\n  presence: {presence}\n  env_file: {env_file}\n"
    )

def new_provider(tenant="aoi", presence="command-chair", env=ENV, context="primary",
                 platform="discord"):
    write_cfg(tenant, presence, env)
    os.environ["HERMES_HOME"] = HOME
    p = MusubiMemoryProvider()
    p.initialize("rp-session", hermes_home=HOME, platform=platform, agent_context=context)
    return p

def ok(label, cond):
    print(f"  {'PASS' if cond else '*** FAIL ***'}  {label}")
    return cond

results = []

print("=" * 70)
print("1. GREEN: a primary turn writes and is VERIFIED BY READ-BACK")
print("=" * 70)
p = new_provider()
rid = p._enqueue("RED-PROOF lifecycle probe: provider write path.", importance=1,
                 tags=["hermes:redproof"], plane="lifecycle")
p._await_quiet(15)
state = p._row_state(rid)
results.append(ok(f"write verified by read-back (state={state})", state == "verified"))
p.shutdown()

print()
print("=" * 70)
print("2. RED: is_available() must be FALSE on broken config (no network call)")
print("=" * 70)
p2 = MusubiMemoryProvider()
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-empty-")
results.append(ok("is_available() False when unconfigured", p2.is_available() is False))

print()
print("=" * 70)
print("3. RED: a NON-PRIMARY context must NOT write (cron cannot author memories)")
print("=" * 70)
p3 = new_provider(context="cron")
rid3 = p3._enqueue("cron must never write this", plane="lifecycle")
results.append(ok("cron write refused (returned None)", rid3 is None))
tool = json.loads(p3.handle_tool_call("musubi_remember", {"content": "cron tries the tool"}))
results.append(ok(f"tool refuses in cron context (status={tool['status']})",
                  tool["ok"] is False and tool["status"] == "refused"))
p3.shutdown()

print()
print("=" * 70)
print("4. RED: MUSUBI DOWN -> the write must be QUEUED and NOT LOST, never 'stored'")
print("=" * 70)
badenv = pathlib.Path(HOME, "dead.env")
tok = [l for l in pathlib.Path(ENV).read_text().splitlines() if l.startswith("MUSUBI_TOKEN=")][0]
badenv.write_text("MUSUBI_API_URL=http://musubi.example.test:8100/v1\n" + tok + "\n")
p4 = new_provider(env=str(badenv))
answer = json.loads(p4.handle_tool_call("musubi_remember", {"content": "network is dead; do not lie to me"}))
print(f"     tool said: {answer}")
results.append(ok("does NOT claim 'stored' when Musubi is unreachable",
                  answer["status"] != "stored"))
results.append(ok("says 'queued' with a durable receipt — honest",
                  answer["status"] == "queued" and bool(answer.get("receipt"))))
h = p4._outbox.health()
results.append(ok(f"the memory is still on disk (pending={h['pending']})", h["pending"] >= 1))
p4.shutdown()

print()
print("=" * 70)
print("5. RED: a 403 (out of scope) must go DEAD, not retry forever")
print("=" * 70)
p5 = new_provider(tenant="tama", presence="command-chair")  # aoi's token cannot write tama's ns
rid5 = p5._enqueue("aoi's token must NOT be able to write into tama's namespace",
                   plane="lifecycle")
p5._await_quiet(10)
state5 = p5._row_state(rid5)
results.append(ok(f"cross-tenant write marked DEAD, not retried forever (state={state5})",
                  state5 == "dead"))
p5.shutdown()

print()
print("=" * 70)
print(f"RESULT: {sum(results)}/{len(results)} gates passed")
print("=" * 70)
sys.exit(0 if all(results) else 1)
