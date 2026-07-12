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

import json
import logging
import os
import re
import sqlite3
import threading
import time
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
                 query: Optional[dict] = None) -> dict:
        url = f"{self.api_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
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

    def write(self, namespace: str, content: str, tags: List[str], importance: int) -> str:
        payload = self._request("POST", "/episodic", body={
            "namespace": namespace,
            "content": content,
            "tags": tags,
            "importance": importance,
        })
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
        namespace    TEXT    NOT NULL,
        content      TEXT    NOT NULL,
        tags         TEXT    NOT NULL,
        importance   INTEGER NOT NULL,
        session_id   TEXT,
        created_at   REAL    NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_try_at  REAL    NOT NULL DEFAULT 0,
        last_error   TEXT,
        state        TEXT    NOT NULL DEFAULT 'pending',  -- pending | verified | dead
        object_id    TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(state, next_try_at);
    """

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
        """Durably accept a write. If THIS fails, the write has failed — say so."""
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT INTO outbox (namespace, content, tags, importance, session_id, "
                "created_at, next_try_at) VALUES (?,?,?,?,?,?,?)",
                (namespace, content, json.dumps(tags), importance, session_id,
                 time.time(), 0.0),
            )
            con.commit()
            return int(cur.lastrowid)

    def claim_batch(self, limit: int = 20) -> List[sqlite3.Row]:
        with self._lock, self._connect() as con:
            con.row_factory = sqlite3.Row
            return list(con.execute(
                "SELECT * FROM outbox WHERE state='pending' AND next_try_at<=? "
                "ORDER BY id LIMIT ?", (time.time(), limit),
            ))

    def mark_verified(self, row_id: int, object_id: str) -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE outbox SET state='verified', object_id=?, last_error=NULL "
                        "WHERE id=?", (object_id, row_id))
            con.commit()

    def mark_failed(self, row_id: int, error: str, *, retryable: bool) -> None:
        """Back off on transient errors. Do NOT retry a 403 for eternity."""
        with self._lock, self._connect() as con:
            if not retryable:
                con.execute("UPDATE outbox SET state='dead', last_error=? WHERE id=?",
                            (error[:500], row_id))
            else:
                row = con.execute("SELECT attempts FROM outbox WHERE id=?", (row_id,)).fetchone()
                attempts = (row[0] if row else 0) + 1
                backoff = min(300.0, (2 ** min(attempts, 8)) + (row_id % 7))  # jittered, capped 5m
                con.execute(
                    "UPDATE outbox SET attempts=?, next_try_at=?, last_error=? WHERE id=?",
                    (attempts, time.time() + backoff, error[:500], row_id),
                )
            con.commit()

    def health(self) -> Dict[str, Any]:
        """What Shiori's Silence Monitor needs: depth, age, and the dead."""
        with self._lock, self._connect() as con:
            pending = con.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
            dead = con.execute("SELECT COUNT(*) FROM outbox WHERE state='dead'").fetchone()[0]
            oldest = con.execute(
                "SELECT MIN(created_at) FROM outbox WHERE state='pending'").fetchone()[0]
        return {
            "pending": pending,
            "dead": dead,
            "oldest_pending_age_s": (time.time() - oldest) if oldest else 0.0,
        }


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

        # Minimal, dependency-free read of the `musubi:` block. Hermes hands us a
        # parsed config in most paths; this is the standalone fallback.
        block = re.search(r"(?ms)^musubi:\s*$(.*?)(?=^\S|\Z)", raw)
        if block:
            for line in block.group(1).splitlines():
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
        return ok

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
        self._context = str(kwargs.get("agent_context") or "primary")

        self._client = MusubiClient(self._cfg["api_url"], self._cfg["token"])
        self._outbox = Outbox(Path(home) / "musubi-outbox.db")

        ns = self._namespace("episodic")
        if not NAMESPACE_RE.match(ns):
            # Fail at startup, loudly. A malformed namespace is a 422 on every single
            # write — i.e. an agent that talks all day and remembers nothing.
            raise ValueError(
                f"musubi: refusing to start with an invalid namespace {ns!r}. "
                f"Must be tenant/presence/plane with plane in {VALID_PLANES}."
            )

        self._stop.clear()
        self._worker = threading.Thread(target=self._drain_loop, name="musubi-outbox",
                                        daemon=True)
        self._worker.start()
        logger.info("musubi: ready — namespace=%s platform=%s context=%s",
                    ns, self._platform, self._context)

    def shutdown(self) -> None:
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
                 tags: Optional[List[str]] = None, plane: str = "episodic") -> Optional[int]:
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
                                          self._tags(tags), importance, self._session_id)
        except Exception as e:
            # A failure to durably ACCEPT the write IS a failed write. Do not pretend.
            self._emit_metric("musubi_memory_writes_total", 1, status="enqueue_failed")
            logger.error("musubi: DURABLE ENQUEUE FAILED — this memory is NOT saved: %s", e)
            raise MusubiError(f"outbox enqueue failed: {e}", retryable=False) from e
        self._emit_metric("musubi_memory_writes_total", 1, status="queued")
        return row_id

    def sync_turn(self, *args, **kwargs) -> None:
        """Called by Hermes per turn. Persist the exchange; never block the turn."""
        user = kwargs.get("user_message") or (args[0] if args else None)
        assistant = kwargs.get("assistant_message") or (args[1] if len(args) > 1 else None)
        if not user and not assistant:
            return
        parts = []
        if user:
            parts.append(f"USER: {str(user).strip()}")
        if assistant:
            parts.append(f"ASSISTANT: {str(assistant).strip()}")
        try:
            self._enqueue("\n\n".join(parts), importance=4, tags=KIND_EPISODE + ["hermes:turn"])
        except MusubiError:
            # Already logged at error. The turn continues — the human keeps talking.
            # But the failure is LOUD in the log and VISIBLE in telemetry.
            pass

    def on_memory_write(self, content: str, **kwargs) -> None:
        try:
            self._enqueue(content, importance=int(kwargs.get("importance", 6)),
                          tags=KIND_EPISODE + ["hermes:explicit"])
        except MusubiError:
            pass

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not messages:
            return
        try:
            self._enqueue(
                f"SESSION CLOSE {self._session_id} — {len(messages)} messages.",
                importance=6, tags=KIND_EPISODE + ["hermes:session-close"],
            )
        except MusubiError:
            pass
        # Give the worker a chance to land it before the process exits.
        self._drain_once(deadline=time.time() + 8.0)

    # ---- the worker: enqueue -> POST -> READ BACK BY ID -> verified ----------

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_once()
            except Exception as e:  # a worker that dies silently is the whole nightmare
                logger.error("musubi: outbox worker error: %s", e)
            self._stop.wait(2.0)

    def _drain_once(self, deadline: Optional[float] = None) -> None:
        if not (self._outbox and self._client):
            return
        while True:
            batch = self._outbox.claim_batch()
            if not batch:
                return
            for row in batch:
                if deadline and time.time() > deadline:
                    return
                self._deliver(row)
            if not deadline:
                return

    def _deliver(self, row: sqlite3.Row) -> None:
        assert self._client and self._outbox
        try:
            object_id = self._client.write(
                row["namespace"], row["content"],
                json.loads(row["tags"]), int(row["importance"]),
            )
        except MusubiError as e:
            self._outbox.mark_failed(row["id"], str(e), retryable=e.retryable)
            self._emit_metric("musubi_memory_writes_total", 1,
                              status="failed_permanent" if not e.retryable else "failed_retryable")
            level = logger.error if not e.retryable else logger.warning
            level("musubi: write %s (%s): %s", "DEAD" if not e.retryable else "deferred",
                  row["namespace"], e)
            return

        # THE ONLY EVIDENCE THAT COUNTS. Aoi lost a full day's lesson to a tool that
        # echoed her input back like it had worked. An accepted POST is a rumour;
        # the object coming back by id is the fact.
        try:
            self._client.read_back(row["namespace"], object_id)
        except MusubiError as e:
            self._outbox.mark_failed(row["id"], f"readback failed: {e}", retryable=True)
            self._emit_metric("musubi_memory_writes_total", 1, status="unverified")
            logger.error("musubi: WROTE BUT COULD NOT VERIFY %s — treating as NOT stored: %s",
                         object_id, e)
            return

        self._outbox.mark_verified(row["id"], object_id)
        self._emit_metric("musubi_memory_writes_total", 1, status="success")
        logger.debug("musubi: verified %s -> %s", row["namespace"], object_id)

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
                "input_schema": {
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
                "input_schema": {
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
        if tool_name == "musubi_remember":
            if not self._writable():
                return ("REFUSED: not a primary turn. Cron and subagent contexts do not "
                        "write to a person's memory.")
            try:
                row_id = self._enqueue(args["content"],
                                       importance=int(args.get("importance", 6)),
                                       tags=KIND_EPISODE + ["hermes:explicit"])
            except MusubiError as e:
                return f"FAILED: the memory was NOT stored — {e}"
            if row_id is None:
                return "Nothing to store."
            # Give it a moment to land so the honest answer is usually 'stored'.
            self._drain_once(deadline=time.time() + 3.0)
            state = self._row_state(row_id)
            if state == "verified":
                return "stored (verified by read-back)"
            if state == "dead":
                return "FAILED: permanently rejected by the memory plane. NOT stored."
            return ("queued — durably on disk, not yet confirmed by the memory plane. "
                    "It will be retried until it is verified.")

        if tool_name == "musubi_recall":
            if not self._client:
                return "memory unavailable"
            try:
                payload = self._client.retrieve(
                    self._namespace("episodic"), mode=MODE_QUERY,
                    limit=int(args.get("limit", 5)), query_text=args["query"],
                )
            except MusubiError as e:
                return f"recall failed: {e}"
            items = payload.get("results") or payload.get("data") or []
            if not items:
                return "No relevant memories."
            return "\n".join(f"- {(i.get('content') or '').strip()[:400]}" for i in items)

        return f"unknown tool {tool_name}"

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
        """Emit to whatever Hermes' hook system has wired up; always log.

        Shiori's Silence Monitor needs writes/turn, not just errors. An agent that is
        asleep writes nothing and is healthy. An agent that is TALKING and writing
        nothing has amnesia. The `context` label is what separates those two — without
        it, her alert cries wolf on every cron run and gets muted. A muted monitor is
        how a friend forgets for three weeks and nobody notices.
        """
        labels = {"tenant": self._tenant, "presence": self._presence,
                  "context": self._context, "platform": self._platform, **labels}
        logger.info("musubi.metric %s=%d %s", metric, value,
                    " ".join(f"{k}={v}" for k, v in labels.items()))
        hook = getattr(self, "_otel_hook", None)
        if hook:
            try:
                hook(metric, value, labels)
            except Exception as e:  # telemetry must never take the agent down
                logger.debug("musubi: otel hook failed: %s", e)

    # ---- config surface -----------------------------------------------------

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
        return [str(Path(home) / "musubi-outbox.db")]
