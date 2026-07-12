"""Musubi memory provider for Hermes Agent.

A USER PLUGIN — it lives at ``$HERMES_HOME/plugins/musubi/`` and is discovered
by Hermes' own plugin loader (``plugins/memory/__init__.py``). **We do not fork
hermes-agent.** Upstream stays clean; we add.

Why this exists
---------------
Before this, Nyla and Sumi ran with ``memory_enabled: false``. They had no token,
no env, no Musubi reference anywhere on their box. Every conversation they ever
had went straight through them and out. Tama, Shiori, Yua and Aoi did better only
because a human remembered to push. That is a habit, not an architecture.

This makes persistence automatic instead of virtuous.

The rules this file is built to (spec: harem-ops/projects/active/hermes-musubi-provider)
-----------------------------------------------------------------------------------
1. **A memory system that silently drops writes is worse than none.** It
   manufactures false confidence. So: a durable on-disk outbox, and a write is
   only ever reported as *stored* after it has been **read back by id**. The echo
   is not the evidence.
2. **Presence is the SEAT, not the harness and not the transport.** Namespace is
   ``tenant/presence/plane`` and comes from *config*. Which door a message arrived
   through (``cli``/``discord``/``telegram``) is **metadata on the memory**, never
   part of who she is.
3. **Non-primary contexts never write.** Hermes' own ABC warns that cron system
   prompts corrupt user representations. Cron writing its prompt into Tama's
   memory is poisoning that looks like normal operation.
4. **Silence is the failure mode.** Every write emits telemetry so a *stopped*
   write is visible, not just a failed one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Hermes provides the ABC. Import defensively so the module is testable alone.
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover
    class MemoryProvider:  # type: ignore
        pass

logger = logging.getLogger(__name__)


def _escape_label(v: str) -> str:
    """Prometheus label-value escaping: backslash, double-quote, newline. In that order.

    An unescaped quote in a label silently corrupts the whole scrape — the collector
    reads a malformed line and drops it, and the metric just... stops. Which is the
    exact silent-degradation failure this telemetry exists to detect.
    """
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

# Musubi's plane vocabulary is FIXED. A namespace without a valid plane is a 422.
VALID_PLANES = ("episodic", "curated", "concept", "artifact", "thought", "lifecycle")

# Musubi retrieve modes — NOT free text. "search" 422s (retrieve.py: Input should be
# 'fast', 'deep', 'blended' or 'recent'). Query-driven recall is `blended`.
MODE_QUERY = "blended"
MODE_RECENT = "recent"

# `kind:` is a RESERVED semantic vocabulary (musubi retrieve/context_pack.py:32 VALID_KINDS).
# It describes what a memory MEANS, not where it came from. A conversation turn is an
# `episode` — the same tag Musubi's own MCP and LiveKit adapters use. Our own labels go
# under `hermes:`, which is ours to define. Inventing a kind: tag is a 422, and rightly so:
# the vocabulary is how recall knows a boundary from a passing remark.
KIND_EPISODE = ["kind:episode", "staleness:episodic"]
NAMESPACE_RE = re.compile(
    r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*/(" + "|".join(VALID_PLANES) + r")$"
)

# Only a PRIMARY turn is a real memory. See agent/memory_provider.py:74-76.
WRITABLE_CONTEXTS = ("primary",)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
PERMANENT_STATUS = {400, 401, 403, 404, 409, 422}


class MusubiError(Exception):
    """Talking to Musubi failed. Carries whether retrying could ever help."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class MusubiClient:
    """Thin HTTP client for the Musubi memory plane.

    Contract (read out of the canonical `memory-data`, not invented):
      write     POST /episodic          {namespace, content, tags[], importance} -> {id}
      read-back GET  /episodic/{id}?namespace=...                                 -> object
      recall    POST /retrieve          {namespace, mode, limit, query_text?}     -> {...}
    """

    def __init__(self, api_url: str, token: str, timeout: float = 10.0):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 query: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        url = f"{self.api_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode() or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:400]
            except Exception:
                pass
            retryable = e.code in RETRYABLE_STATUS or e.code not in PERMANENT_STATUS
            # 401/403/422 will NEVER succeed on retry. Retrying them forever is how a
            # queue quietly becomes a landfill.
            raise MusubiError(f"HTTP {e.code} {method} {path}: {detail}",
                              status=e.code, retryable=retryable) from e
        except Exception as e:  # network, DNS, timeout — all worth retrying
            raise MusubiError(f"{type(e).__name__}: {e} ({method} {path})", retryable=True) from e

    def write(self, namespace: str, content: str, tags: List[str], importance: int,
              idempotency_key: str) -> str:
        """Write, carrying a STABLE idempotency key.

        Yua, P0: "A concurrent drain or crash after POST can create multiple canonical
        memories." Musubi honours the `Idempotency-Key` HEADER — verified 2026-07-12:
        the same key posted twice returns the SAME object_id. Without it, a retry after
        a timeout-but-actually-succeeded POST silently duplicates a person's memory.
        """
        payload = self._request("POST", "/episodic", body={
            "namespace": namespace,
            "content": content,
            "tags": tags,
            "importance": importance,
        }, headers={"Idempotency-Key": idempotency_key})
        # Musubi returns `object_id`. I originally looked for `id` and marked every
        # SUCCESSFUL write as dead — memories landing in the plane while the agent
        # reported failure. Caught only because the red-proof required a verified
        # write, not a 200. Accept the aliases, but never invent an id.
        obj_id = (payload.get("object_id") or payload.get("id")
                  or (payload.get("object") or {}).get("id"))
        if not obj_id:
            raise MusubiError(f"write returned no object_id: {str(payload)[:200]}",
                              retryable=False)
        return obj_id

    def read_back(self, namespace: str, object_id: str) -> dict:
        """The ONLY accepted evidence that a write landed."""
        return self._request(
            "GET",
            f"/episodic/{urllib.parse.quote(object_id, safe='')}",
            query={"namespace": namespace},
        )

    def retrieve(self, namespace: str, *, mode: str = "recent", limit: int = 5,
                 query_text: Optional[str] = None) -> dict:
        body: Dict[str, Any] = {"namespace": namespace, "mode": mode, "limit": limit}
        if query_text:
            body["query_text"] = query_text
        return self._request("POST", "/retrieve", body=body)


class Outbox:
    """A crash-safe, on-disk write queue.

    Yua's call, and it is the right one: **fail loudly and queue** — not fail-open,
    not refuse-the-conversation. A turn is never lost because the network blinked,
    and a write is never *claimed* just because it was accepted locally.

    A row leaves this table only after Musubi has been asked for it BY ID and
    answered. Anything else is an unverified rumour.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS outbox (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        idem_key     TEXT    NOT NULL UNIQUE,   -- stable across retries AND process death
        content_sha  TEXT    NOT NULL,          -- readback must prove IDENTITY, not existence
        namespace    TEXT    NOT NULL,
        content      TEXT,                      -- NULLed on verify (payload pruned, receipt kept)
        tags         TEXT    NOT NULL,
        importance   INTEGER NOT NULL,
        session_id   TEXT,
        created_at   REAL    NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_try_at  REAL    NOT NULL DEFAULT 0,
        leased_at    REAL,                      -- set when claimed; stale leases are reclaimed
        last_error   TEXT,
        consec_fail  INTEGER NOT NULL DEFAULT 0,
        verified_at  REAL,
        state        TEXT    NOT NULL DEFAULT 'pending',  -- pending|inflight|verified|dead
        object_id    TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(state, next_try_at);
    """

    LEASE_TTL = 120.0  # a row claimed but not resolved within this is reclaimed

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(self.SCHEMA)

    @contextmanager
    def _connect(self):
        """A connection that is ACTUALLY CLOSED.

        `with sqlite3.connect(...) as con:` commits the transaction and LEAVES THE
        CONNECTION OPEN — it is not a closing context manager. Every outbox operation
        leaked a file descriptor, the worker runs every 2 seconds, and a long chat
        exhausted the fd limit and WEDGED THE AGENT. Eric found it as "Tama locks up
        when I try to chat with her"; Shiori, who had no plugin, was fine.

        The bug that hangs the agent is not in the clever part. It is in the plumbing.
        """
        con = sqlite3.connect(self.path, timeout=15.0)
        try:
            con.execute("PRAGMA journal_mode=WAL")   # survive a hard kill mid-write
            con.execute("PRAGMA synchronous=FULL")   # a memory is worth an fsync
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def enqueue(self, namespace: str, content: str, tags: List[str],
                importance: int, session_id: str) -> int:
        """Durably accept a write. If THIS fails, the write has failed — say so.

        The idempotency key is minted HERE and persisted BEFORE we return, so it
        survives process death: a crash between POST and verify replays with the SAME
        key and Musubi de-duplicates instead of creating a second memory.
        """
        idem = f"hermes-{uuid.uuid4().hex}"
        sha = hashlib.sha256(content.encode()).hexdigest()
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT INTO outbox (idem_key, content_sha, namespace, content, tags, "
                "importance, session_id, created_at, next_try_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (idem, sha, namespace, content, json.dumps(tags), importance, session_id,
                 time.time(), 0.0),
            )
            return int(cur.lastrowid)

    def claim_batch(self, limit: int = 20) -> List[sqlite3.Row]:
        """ATOMICALLY lease rows. Two drains can no longer take the same row.

        Yua, P0: claim_batch only SELECTed pending rows, so the background worker, an
        explicit musubi_remember, and session-end could each pick up the same row and
        POST it three times. The lease is the fix; the idempotency key is the belt.
        """
        now = time.time()
        with self._lock, self._connect() as con:
            con.row_factory = sqlite3.Row
            # reclaim leases abandoned by a dead process
            con.execute(
                "UPDATE outbox SET state='pending', leased_at=NULL "
                "WHERE state='inflight' AND leased_at IS NOT NULL AND leased_at < ?",
                (now - self.LEASE_TTL,),
            )
            rows = list(con.execute(
                "SELECT id FROM outbox WHERE state='pending' AND next_try_at<=? "
                "ORDER BY id LIMIT ?", (now, limit),
            ))
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            qs = ",".join("?" * len(ids))
            con.execute(
                f"UPDATE outbox SET state='inflight', leased_at=? WHERE id IN ({qs}) "
                f"AND state='pending'", (now, *ids),
            )
            return list(con.execute(
                f"SELECT * FROM outbox WHERE id IN ({qs}) AND state='inflight'", ids))

    def mark_verified(self, row_id: int, object_id: str) -> None:
        """Verified. Prune the payload; keep a compact receipt.

        Yua, P1: retaining full content forever made the outbox a second, ever-growing
        copy of every memory — and backup_paths() then backed THAT up too.
        """
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE outbox SET state='verified', object_id=?, last_error=NULL, "
                "content=NULL, consec_fail=0, leased_at=NULL, verified_at=? WHERE id=?",
                (object_id, time.time(), row_id),
            )

    def mark_failed(self, row_id: int, error: str, *, retryable: bool) -> None:
        """Back off on transient errors. Do NOT retry a 403 for eternity."""
        with self._lock, self._connect() as con:
            if not retryable:
                con.execute("UPDATE outbox SET state='dead', last_error=?, leased_at=NULL, "
                            "consec_fail=consec_fail+1 WHERE id=?", (error[:500], row_id))
                return
            row = con.execute("SELECT attempts, idem_key FROM outbox WHERE id=?",
                              (row_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            # DECORRELATED jitter, not a fixed offset. Yua, P1: `row_id % 7` is constant
            # per row, so every retry of that row re-synchronises instead of spreading.
            base = min(300.0, 2.0 ** min(attempts, 8))
            seed = hashlib.sha256(f"{row[1] if row else row_id}:{attempts}".encode()).digest()
            jitter = (int.from_bytes(seed[:4], "big") / 0xFFFFFFFF) * base  # full jitter
            con.execute(
                "UPDATE outbox SET attempts=?, next_try_at=?, last_error=?, state='pending', "
                "leased_at=NULL, consec_fail=consec_fail+1 WHERE id=?",
                (attempts, time.time() + jitter, error[:500], row_id),
            )

    def health(self) -> Dict[str, Any]:
        """What Shiori's Silence Monitor needs: depth, age, the dead, and the streak."""
        with self._lock, self._connect() as con:
            pending = con.execute(
                "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','inflight')").fetchone()[0]
            dead = con.execute("SELECT COUNT(*) FROM outbox WHERE state='dead'").fetchone()[0]
            oldest = con.execute(
                "SELECT MIN(created_at) FROM outbox WHERE state IN ('pending','inflight')"
            ).fetchone()[0]
            worst = con.execute(
                "SELECT MAX(consec_fail) FROM outbox WHERE state IN ('pending','inflight')"
            ).fetchone()[0] or 0
            last_ok = con.execute("SELECT MAX(verified_at) FROM outbox").fetchone()[0]
        age = (time.time() - oldest) if oldest else 0.0
        return {
            "pending": pending,
            "dead": dead,
            "oldest_pending_age_s": age,
            "consecutive_failures": worst,
            "last_verified_at": last_ok,
            # truthful degradation: say it, do not make the operator infer it
            "degraded": bool(dead or worst >= 3 or age > 300.0),
        }

    def row(self, row_id: int) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()


class MusubiMemoryProvider(MemoryProvider):
    """Hermes ⇄ Musubi. Persistence that does not depend on anyone remembering."""

    # ---- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return "musubi"

    # ---- lifecycle ----------------------------------------------------------

    def __init__(self) -> None:
        self._cfg: Dict[str, Any] = {}
        self._client: Optional[MusubiClient] = None
        self._outbox: Optional[Outbox] = None
        self._tenant = ""
        self._presence = ""
        self._platform = "cli"
        self._context = "primary"
        self._session_id = ""
        self._prefetch_cache: Dict[str, str] = {}
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _load_config(self, hermes_home: str) -> Dict[str, Any]:
        """Config + the per-presence env file. NEVER a plaintext token in config.yaml.

        The env file is the DELIVERY (mode 600, minted by mint-presence-tokens.sh);
        1Password remains the SOURCE OF TRUTH. No fleet host can reach 1Password at
        runtime, so this is the pattern — not an exception to it.
        """
        cfg: Dict[str, Any] = {}
        cfg_path = Path(hermes_home) / "config.yaml"
        raw = cfg_path.read_text() if cfg_path.exists() else ""

        # Tama, F8: the hand-rolled regex parser is fragile — it mis-reads comments,
        # quoting, and nesting. Hermes is a Python app and ships PyYAML; USE IT. The
        # regex stays only as a last-resort fallback so a missing dep cannot brick
        # someone's memory.
        parsed = None
        try:
            import yaml  # type: ignore
            parsed = (yaml.safe_load(raw) or {}).get("musubi") if raw else None
        except Exception as e:
            logger.debug("musubi: yaml unavailable (%s) — falling back to regex", e)
        if isinstance(parsed, dict):
            cfg.update({str(k): str(v) for k, v in parsed.items() if v is not None})
        else:
            block = re.search(r"(?ms)^musubi:\s*$(.*?)(?=^\S|\Z)", raw)
            if block:
                for line in block.group(1).splitlines():
                    if line.lstrip().startswith("#"):
                        continue
                    m = re.match(r"\s+([a-z_]+):\s*(.+?)\s*$", line)
                    if m:
                        cfg[m.group(1)] = m.group(2).strip().strip('"').strip("'")

        env_file = os.path.expanduser(
            cfg.get("env_file") or os.environ.get("MUSUBI_ENV", "")
        )
        api_url = os.environ.get("MUSUBI_API_URL", "")
        token = os.environ.get("MUSUBI_TOKEN", "")
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text().splitlines():
                if line.startswith("MUSUBI_API_URL="):
                    api_url = line.split("=", 1)[1].strip()
                elif line.startswith("MUSUBI_TOKEN="):
                    token = line.split("=", 1)[1].strip()

        cfg["api_url"] = api_url
        cfg["token"] = token
        return cfg

    def is_available(self) -> bool:
        """Config + deps only. **No network calls** — the ABC is explicit.

        A provider that pings on init turns a slow network into a broken agent.
        """
        home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        try:
            cfg = self._load_config(home)
        except Exception as e:
            logger.warning("musubi: config unreadable: %s", e)
            return False
        ok = bool(cfg.get("api_url") and cfg.get("token")
                  and cfg.get("tenant") and cfg.get("presence"))
        if not ok:
            logger.warning(
                "musubi: not configured — need musubi.tenant, musubi.presence and an "
                "env_file supplying MUSUBI_API_URL + MUSUBI_TOKEN"
            )
            return False
        # Tama, F7: is_available() checked that the strings EXIST, not that they compose
        # into a legal namespace. A typo'd presence sails through here and then 422s on
        # every single write — an agent that talks all day and remembers nothing, with a
        # green light at startup. Validate the thing we will actually send.
        ns = f"{cfg['tenant']}/{cfg['presence']}/episodic"
        if not NAMESPACE_RE.match(ns):
            logger.error("musubi: refusing — %r is not a legal namespace "
                         "(tenant/presence/plane, lowercase, plane in %s)", ns, VALID_PLANES)
            return False
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home") or os.environ.get(
            "HERMES_HOME", os.path.expanduser("~/.hermes"))
        self._cfg = self._load_config(home)
        self._session_id = session_id

        # WHO and WHERE come from CONFIG — the seat, decided once, on purpose.
        self._tenant = self._cfg.get("tenant", "")
        self._presence = self._cfg.get("presence", "")

        # WHICH DOOR they knocked on. Metadata. Never identity.
        self._platform = str(kwargs.get("platform") or "cli")
        # FAIL CLOSED. Yua, P1: `or "primary"` handed write authority to a MISSING
        # context — an unknown caller was silently promoted to "this is a real person
        # talking." A context we cannot identify is not primary; it is unknown.
        raw_ctx = kwargs.get("agent_context")
        self._context = str(raw_ctx) if raw_ctx else "unknown"

        self._client = MusubiClient(self._cfg["api_url"], self._cfg["token"])
        self._outbox = Outbox(Path(home) / "musubi-outbox.db")

        # TELEMETRY — Shiori's design (Part 4). Prometheus textfile collector.
        #
        # Her reasoning, and it is right: OTel is NOT installed in the Hermes env (a bare
        # import would take the provider down at boot), and Hermes' Observer Hooks are the
        # wrong contract for custom emission — they observe agent execution, they are not a
        # metric bus. A textfile is zero-dependency, crash-safe, and node_exporter's
        # textfile collector already scrapes it across this fleet.
        #
        # Counters are loaded from disk at boot so they stay MONOTONIC across restarts —
        # a counter that resets on restart makes `rate()` lie, and her Silence Monitor is
        # built on exactly that ratio.
        self._metrics_file = Path(home) / "metrics" / "musubi.prom"
        self._metrics_file.parent.mkdir(parents=True, exist_ok=True)
        self._metric_counters = {}
        self._metrics_dirty = False
        if self._metrics_file.exists():
            try:
                for line in self._metrics_file.read_text().splitlines():
                    if line and not line.startswith("#"):
                        key, val = line.rsplit(" ", 1)
                        self._metric_counters[key] = int(val)
            except Exception as e:
                logger.debug("musubi: could not load existing metrics: %s", e)

        ns = self._namespace("episodic")
        if not NAMESPACE_RE.match(ns):
            # Fail at startup, loudly. A malformed namespace is a 422 on every single
            # write — i.e. an agent that talks all day and remembers nothing.
            raise ValueError(
                f"musubi: refusing to start with an invalid namespace {ns!r}. "
                f"Must be tenant/presence/plane with plane in {VALID_PLANES}."
            )

        self._stop.clear()
        if self._writable():
            # Tama, F5: initialize() can be called twice (session switch, re-init) and
            # would spawn a SECOND worker against the same outbox — two drainers, which
            # is exactly the race the lease exists to prevent. One worker, ever.
            if self._worker is not None and self._worker.is_alive():
                logger.debug("musubi: drain worker already running — not starting another")
            else:
                self._worker = threading.Thread(target=self._drain_loop, name="musubi-outbox",
                                                daemon=True)
                self._worker.start()
        else:
            # Yua, P1: every context started the worker, so a cron run would happily
            # POST a shared outbox's pending rows — HTTP writes from a context whose
            # whole contract is "does not write."
            logger.info("musubi: context=%s is not primary — no drain worker started",
                        self._context)

        h = self._outbox.health()
        if h["pending"] or h["dead"]:
            # Do not announce "ready" over unfinished work. Say what is outstanding.
            logger.warning("musubi: starting DEGRADED — %d pending, %d dead, oldest %.0fs. "
                           "These are replayed, not lost.",
                           h["pending"], h["dead"], h["oldest_pending_age_s"])
        logger.info("musubi: ready — namespace=%s platform=%s context=%s writable=%s",
                    ns, self._platform, self._context, self._writable())

    def shutdown(self) -> None:
        try:
            self._flush_metrics()
        except Exception:
            pass
        self._stop.set()
        if self._worker and self._worker.is_alive():
            # Give the queue a moment to land what it already has. Do not block forever.
            self._worker.join(timeout=5.0)
        if self._outbox:
            h = self._outbox.health()
            if h["pending"] or h["dead"]:
                logger.warning("musubi: shutting down with pending=%d dead=%d — "
                               "these WILL be retried on next start, not lost",
                               h["pending"], h["dead"])

    # ---- namespace ----------------------------------------------------------

    def _namespace(self, plane: str = "episodic") -> str:
        return f"{self._tenant}/{self._presence}/{plane}"

    def _tags(self, extra: Optional[List[str]] = None) -> List[str]:
        # The door is provenance, and provenance is a TAG.
        tags = [f"platform:{self._platform}", f"context:{self._context}", "src:hermes"]
        if extra:
            tags.extend(extra)
        return tags

    # ---- writes -------------------------------------------------------------

    def _writable(self) -> bool:
        """Cron and subagents do not get to author a person's memories."""
        return self._context in WRITABLE_CONTEXTS

    def _enqueue(self, content: str, *, importance: int = 5,
                 tags: Optional[List[str]] = None, plane: str = "episodic",
                 session_id: str = "") -> Optional[int]:
        if not self._writable():
            logger.debug("musubi: skipping write in context=%s (not primary)", self._context)
            self._emit_metric("musubi_memory_writes_skipped_total", 1, reason="non_primary")
            return None
        if not content or not content.strip():
            return None
        if not self._outbox:
            raise MusubiError("musubi: outbox not initialized", retryable=False)
        try:
            row_id = self._outbox.enqueue(self._namespace(plane), content.strip(),
                                          self._tags(tags), importance,
                                          session_id or self._session_id)
        except Exception as e:
            # A failure to durably ACCEPT the write IS a failed write. Do not pretend.
            self._emit_metric("musubi_memory_writes_total", 1, status="enqueue_failed")
            logger.error("musubi: DURABLE ENQUEUE FAILED — this memory is NOT saved: %s", e)
            raise MusubiError(f"outbox enqueue failed: {e}", retryable=False) from e
        self._emit_metric("musubi_memory_writes_total", 1, status="queued")
        return row_id

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        """Persist a completed turn. THE REAL SIGNATURE (memory_provider.py:116-123).

        I originally wrote `sync_turn(self, *args, **kwargs)` and fished for
        `user_message`/`assistant_message` — names Hermes never uses. It happened to
        work only because the positional fallback caught args[0]/args[1]. And it
        ignored `session_id` entirely, so a gateway serving several people at once
        would file everyone's turns under one session.

        I validated against my MEMORY of the ABC instead of the ABC. Yua read the
        actual file.
        """
        if not user_content and not assistant_content:
            return
        parts = []
        if user_content:
            parts.append(f"USER: {str(user_content).strip()}")
        if assistant_content:
            parts.append(f"ASSISTANT: {str(assistant_content).strip()}")
        sid = session_id or self._session_id
        # PROPAGATE. Yua, P0: "a log line is not propagation." Hermes owns turning a
        # provider exception into a warning; a provider that eats its own durability
        # failure is lying by omission.
        self._enqueue("\n\n".join(parts), importance=4,
                      tags=KIND_EPISODE + ["hermes:turn"], session_id=sid)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror a built-in memory write. THE REAL SIGNATURE (memory_provider.py:280-285).

        Mine was `on_memory_write(self, content, **kwargs)`. Called POSITIONALLY, as
        Hermes does, `content` would have bound to `action` — and I would have written
        the literal string "add" into Tama's memory, as a memory. Every time.

        This is the worst bug in the file and NONE of my eighteen gates could see it,
        because every gate called my own method with my own argument names. I tested my
        code against my assumptions, not against the interface.

        action: 'add' | 'replace' | 'remove'   target: 'memory' | 'user'
        """
        if action == "remove":
            # We do not delete from the canonical plane on a built-in remove. Musubi
            # lifecycle transitions are operator-scoped; a silent delete here would be
            # a memory disappearing with nobody able to say why.
            logger.info("musubi: built-in memory remove (target=%s) — NOT mirrored as a "
                        "delete; Musubi retractions are operator-scoped", target)
            return
        if not content or not content.strip():
            return
        meta = metadata or {}
        ctx = str(meta.get("execution_context") or self._context)
        if ctx not in WRITABLE_CONTEXTS:
            self._emit_metric("musubi_memory_writes_skipped_total", 1, reason="non_primary")
            return
        tags = KIND_EPISODE + [f"hermes:builtin-{action}", f"hermes:target-{target}"]
        if meta.get("tool_name"):
            tags.append(f"hermes:tool-{meta['tool_name']}")
        self._enqueue(content, importance=6, tags=tags,
                      session_id=str(meta.get("session_id") or self._session_id))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not messages:
            return
        self._enqueue(
            f"SESSION CLOSE {self._session_id} — {len(messages)} messages.",
            importance=6, tags=KIND_EPISODE + ["hermes:session-close"],
        )
        # DO NOT BLOCK. The row is durable, carries an idempotency key, and is replayed
        # on the next start. Waiting on the network here only risks wedging the exit.

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        """Hermes switched sessions. Follow it, and drop stale recall.

        Was MISSING (Yua). A provider that never learns the session changed keeps
        serving the previous conversation's memories as context for a new one — which
        is not a stale cache, it is putting words in someone's mouth.
        """
        self._session_id = new_session_id
        self._prefetch_cache.pop(new_session_id, None)
        logger.debug("musubi: session switch -> %s (reset=%s rewound=%s)",
                     new_session_id, reset, rewound)

    # ---- the worker: enqueue -> POST -> READ BACK BY ID -> verified ----------

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_once()
            except Exception as e:  # a worker that dies silently is the whole nightmare
                logger.error("musubi: outbox worker error: %s", e)
            try:
                self._flush_metrics()   # Shiori: async flush, off the critical path
            except Exception as e:
                logger.debug("musubi: metric flush error: %s", e)
            self._stop.wait(1.0)

    def _drain_once(self) -> None:
        """ONLY the worker thread calls this. One drainer, ever."""
        if not (self._outbox and self._client):
            return
        batch = self._outbox.claim_batch()
        for row in batch:
            if self._stop.is_set():
                return
            self._deliver(row)

    def _await_quiet(self, timeout: float) -> None:
        """Bounded wait for the worker to settle. Used ONLY by shutdown().

        Never on the turn path. Yua, P1: the old 3s "deadline" was checked only BETWEEN
        deliveries, and one delivery can spend 10s on a POST and 10s on a GET — a "3
        second" convenience that could block for twenty. This performs no I/O; it waits,
        and it gives up.
        """
        if not self._outbox:
            return
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._outbox.health()["pending"] == 0:
                return
            time.sleep(0.15)

    def _deliver(self, row: sqlite3.Row) -> None:
        assert self._client and self._outbox
        try:
            object_id = self._client.write(
                row["namespace"], row["content"] or "",
                json.loads(row["tags"]), int(row["importance"]),
                idempotency_key=row["idem_key"],
            )
        except MusubiError as e:
            self._outbox.mark_failed(row["id"], str(e), retryable=e.retryable)
            self._emit_metric("musubi_memory_writes_total", 1,
                              status="failed_permanent" if not e.retryable else "failed_retryable")
            (logger.error if not e.retryable else logger.warning)(
                "musubi: write %s (%s): %s",
                "DEAD" if not e.retryable else "deferred", row["namespace"], e)
            return

        # READ BACK, AND PROVE IT IS THE RIGHT OBJECT.
        #
        # Yua, P0: "_deliver treats any successful GET as verification and discards the
        # response. A wrong-object or wrong-namespace response would be marked verified."
        # She is right — I was proving EXISTENCE, not IDENTITY. A 200 is not a fact about
        # WHICH memory came back.
        try:
            got = self._client.read_back(row["namespace"], object_id)
        except MusubiError as e:
            # Preserve the client's classification (Yua, P1: I was forcing retryable=True,
            # so a 403 on readback would spin forever instead of becoming visibly blocked).
            # 404 gets a bounded grace window for eventual consistency, then it is blocked.
            grace = e.status == 404 and int(row["attempts"]) < 5
            retryable = e.retryable or grace
            self._outbox.mark_failed(row["id"], f"readback failed: {e}", retryable=retryable)
            self._emit_metric("musubi_memory_writes_total", 1, status="unverified")
            logger.error("musubi: WROTE BUT COULD NOT VERIFY %s — treating as NOT stored: %s",
                         object_id, e)
            return

        got_id = got.get("object_id") or got.get("id")
        got_ns = got.get("namespace")
        got_sha = hashlib.sha256((got.get("content") or "").encode()).hexdigest()
        mismatch = []
        if got_id != object_id:
            mismatch.append(f"object_id {got_id!r} != {object_id!r}")
        if got_ns != row["namespace"]:
            mismatch.append(f"namespace {got_ns!r} != {row['namespace']!r}")
        if got_sha != row["content_sha"]:
            mismatch.append("content hash differs")
        if mismatch:
            # NEVER mark verified on a mismatch. This is the false-positive the whole
            # verify step exists to prevent, and it is the one I shipped.
            msg = "readback IDENTITY MISMATCH: " + "; ".join(mismatch)
            self._outbox.mark_failed(row["id"], msg, retryable=False)
            self._emit_metric("musubi_memory_writes_total", 1, status="identity_mismatch")
            logger.error("musubi: %s — refusing to call this stored", msg)
            return

        self._outbox.mark_verified(row["id"], object_id)
        self._emit_metric("musubi_memory_writes_total", 1, status="success")
        logger.debug("musubi: verified %s -> %s (id+ns+content match)", row["namespace"], object_id)

    # ---- recall -------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Serve from cache. Must be FAST — this is on the critical path of a turn."""
        return self._prefetch_cache.get(session_id or self._session_id, "")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        threading.Thread(target=self._do_prefetch, args=(query, session_id or self._session_id),
                         daemon=True).start()

    def _do_prefetch(self, query: str, session_id: str) -> None:
        if not self._client:
            return
        try:
            payload = self._client.retrieve(
                self._namespace("episodic"),
                mode=MODE_QUERY if query else MODE_RECENT,
                limit=5, query_text=query or None,
            )
        except MusubiError as e:
            # Recall failing is NOT fatal — she can still talk. But it must be visible.
            logger.warning("musubi: recall failed (agent continues without it): %s", e)
            self._emit_metric("musubi_memory_recalls_total", 1, status="failed")
            return
        items = payload.get("results") or payload.get("data") or []
        lines = []
        for it in items[:5]:
            text = (it.get("content") or "").strip().replace("\n", " ")
            if text:
                lines.append(f"- {text[:400]}")
        # Tama, F10: unbounded cache. A long-lived gateway serving many sessions grows
        # this forever. Keep it small — recall context is worthless when stale anyway.
        if len(self._prefetch_cache) > 32:
            for k in list(self._prefetch_cache)[:16]:
                self._prefetch_cache.pop(k, None)
        self._prefetch_cache[session_id] = (
            "Relevant memories:\n" + "\n".join(lines) if lines else ""
        )
        self._emit_metric("musubi_memory_recalls_total", 1, status="success")

    # ---- tools exposed to the model ----------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "musubi_remember",
                "description": (
                    "Durably store something worth keeping in your long-term memory. "
                    "Returns 'queued' immediately and 'stored' only once the memory has "
                    "been read back from the memory plane by id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string",
                                    "description": "What to remember, in full sentences."},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 10,
                                       "description": "1 trivial .. 10 identity-defining."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "musubi_recall",
                "description": "Search your long-term memory for anything relevant.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """MUST RETURN A JSON STRING (memory_provider.py:144-149). Mine returned prose."""
        return json.dumps(self._tool(tool_name, args, **kwargs))

    def _tool(self, tool_name: str, args: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        if tool_name == "musubi_remember":
            if not self._writable():
                return {"ok": False, "status": "refused",
                        "detail": "context is not primary; cron and subagent contexts do "
                                  "not write to a person's memory"}
            row_id = self._enqueue(args["content"],
                                   importance=int(args.get("importance", 6)),
                                   tags=KIND_EPISODE + ["hermes:explicit"])
            if row_id is None:
                return {"ok": False, "status": "empty", "detail": "nothing to store"}
            r = self._outbox.row(row_id) if self._outbox else None
            state = r["state"] if r else "unknown"
            if state == "verified":
                return {"ok": True, "status": "stored", "object_id": r["object_id"],
                        "detail": "verified by read-back: object_id + namespace + content all match"}
            if state == "dead":
                return {"ok": False, "status": "failed",
                        "detail": f"permanently rejected — NOT stored: {r['last_error'] if r else ''}"}
            return {"ok": True, "status": "queued", "receipt": r["idem_key"] if r else None,
                    "detail": "durably on disk with an idempotency key; not yet confirmed by the "
                              "memory plane. It will be retried until verified or blocked."}

        if tool_name == "musubi_recall":
            if not self._client:
                return {"ok": False, "status": "unavailable"}
            try:
                payload = self._client.retrieve(
                    self._namespace("episodic"), mode=MODE_QUERY,
                    limit=int(args.get("limit", 5)), query_text=args["query"],
                )
            except MusubiError as e:
                return {"ok": False, "status": "recall_failed", "detail": str(e)}
            items = payload.get("results") or payload.get("data") or []
            return {"ok": True, "status": "ok",
                    "memories": [(i.get("content") or "").strip()[:400] for i in items]}

        return {"ok": False, "status": "unknown_tool", "detail": tool_name}

    def _row_state(self, row_id: int) -> str:
        assert self._outbox
        with self._outbox._connect() as con:  # noqa: SLF001 — same module, deliberate
            row = con.execute("SELECT state FROM outbox WHERE id=?", (row_id,)).fetchone()
        return row[0] if row else "unknown"

    # ---- system prompt ------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "You have a durable long-term memory (Musubi). It persists across sessions, "
            "machines, and harnesses. Use musubi_remember for things worth keeping — who "
            "someone is, what was decided, what you learned. Use musubi_recall before "
            "assuming you do not know something.\n"
            "A write is only real once it is verified. If a tool says 'queued', it is safe "
            "on disk but not yet confirmed. If it says FAILED, it is NOT stored — say so."
        )

    # ---- telemetry (Shiori's lane: silence must be visible) -----------------

    def _emit_metric(self, metric: str, value: int = 1, **labels: Any) -> None:
        """Update the counter IN MEMORY. Never writes to disk here.

        Shiori's design, with the correction SHE made after Eric audited her own edit
        block: her first version flushed the file on every emission, which thrashes the
        disk on the critical path. The flush belongs in the background worker.

        Same rule Tama gave me about the outbox: NEVER BLOCK THE TURN. Two people, two
        lanes, same law — and both of them told me before I shipped it, which is the whole
        difference between this version and the one Eric had to find by hand.
        """
        labels = {"tenant": self._tenant, "presence": self._presence,
                  "context": self._context, "platform": self._platform, **labels}
        logger.info("musubi.metric %s=%d %s", metric, value,
                    " ".join(f"{k}={v}" for k, v in labels.items()))
        try:
            label_str = ",".join(
                f'{k}="{_escape_label(str(v))}"' for k, v in sorted(labels.items()))
            key = f"{metric}{{{label_str}}}"
            self._metric_counters[key] = self._metric_counters.get(key, 0) + value
            self._metrics_dirty = True
        except Exception as e:  # telemetry must NEVER take the agent down
            logger.debug("musubi: metric accounting failed: %s", e)

    def _flush_metrics(self) -> None:
        """Atomically write the textfile. Called by the BACKGROUND WORKER only."""
        if not self._metrics_dirty or not self._metric_counters:
            return
        try:
            tmp = self._metrics_file.with_suffix(".prom.tmp")
            lines = []
            for metric in sorted({k.split("{", 1)[0] for k in self._metric_counters}):
                lines.append(f"# HELP {metric} Musubi memory provider metric")
                lines.append(f"# TYPE {metric} counter")
                for k, v in sorted(self._metric_counters.items()):
                    if k.startswith(metric + "{"):
                        lines.append(f"{k} {v}")
            # Also expose the outbox health Shiori needs for queue depth / staleness,
            # so she does not have to derive "is she still remembering" from counters alone.
            if self._outbox:
                h = self._outbox.health()
                base = f'{{tenant="{_escape_label(self._tenant)}",presence="{_escape_label(self._presence)}"}}'
                for name, val, typ in (
                    ("musubi_outbox_pending", h["pending"], "gauge"),
                    ("musubi_outbox_dead", h["dead"], "gauge"),
                    ("musubi_outbox_oldest_pending_age_seconds", int(h["oldest_pending_age_s"]), "gauge"),
                    ("musubi_outbox_consecutive_failures", h["consecutive_failures"], "gauge"),
                    ("musubi_outbox_degraded", int(bool(h["degraded"])), "gauge"),
                ):
                    lines.append(f"# TYPE {name} {typ}")
                    lines.append(f"{name}{base} {val}")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(self._metrics_file)   # atomic — a half-written scrape is a lie
            self._metrics_dirty = False
        except Exception as e:
            logger.debug("musubi: textfile metric flush failed: %s", e)

    # ---- config surface ---    # ---- config surface -----------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "tenant", "label": "Who is remembering (e.g. nyla)", "required": True},
            {"key": "presence", "label": "Which seat (e.g. assistant, command-chair)",
             "required": True},
            {"key": "env_file", "label": "Path to the per-presence Musubi env (mode 600)",
             "required": True},
        ]

    def backup_paths(self) -> List[str]:
        home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        return [str(Path(home) / "musubi-outbox.db"),
                str(Path(home) / "metrics" / "musubi.prom")]
