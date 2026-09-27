"""Stale Musubi leases must not be retried as a first POST."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import time
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / "musubi" / "__init__.py"


def load_outbox():
    loader = importlib.machinery.SourceFileLoader("hermes_musubi_lease", str(PLUGIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module.Outbox


def test_stale_lease_reclaim_increments_attempts_and_keeps_accepted(tmp_path: Path) -> None:
    Outbox = load_outbox()
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    fresh = outbox.enqueue("aoi/command-chair/episodic", "first", ["kind:episode"], 1, "s")
    accepted = outbox.enqueue("aoi/command-chair/episodic", "already", ["kind:episode"], 1, "s")
    assert outbox.claim_batch()
    stale = time.time() - Outbox.LEASE_TTL - 5
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as con:
        con.execute(
            "UPDATE outbox SET leased_at=? WHERE id=?",
            (stale, fresh),
        )
        con.execute(
            "UPDATE outbox SET state='inflight', object_id='obj-1', leased_at=? WHERE id=?",
            (stale, accepted),
        )

    claimed = {row["id"]: row for row in outbox.claim_batch()}
    assert claimed[fresh]["attempts"] == 1
    assert claimed[fresh]["object_id"] is None
    assert claimed[accepted]["attempts"] == 1
    assert claimed[accepted]["object_id"] == "obj-1"
    assert claimed[accepted]["state"] == "inflight"


def test_fresh_lease_is_not_reclaimed(tmp_path: Path) -> None:
    Outbox = load_outbox()
    db = tmp_path / "outbox.sqlite3"
    owner = Outbox(db)
    row_id = owner.enqueue("aoi/command-chair/episodic", "held", ["kind:episode"], 1, "s")
    held = owner.claim_batch()
    assert [row["id"] for row in held] == [row_id]
    other = Outbox(db)
    assert other.claim_batch() == []
    assert owner.row(row_id)["lease_owner"] == owner.owner
