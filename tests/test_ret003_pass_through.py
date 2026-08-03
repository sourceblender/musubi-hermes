"""RET-003 pass-through conformance tests for the actual Hermes user plugin.

Per Yua 2026-07-13 12:45:46 #5: the actual fleet-tools plugin
``musubi_recall`` previously returned only object_id, score, content
and STRIPPED plane, namespace, state, importance, score_kind,
provenance_score, and score_components. This test exercises the
plugin transform against fake ranked and recent server payloads,
asserting the new pass-through shape.

Per Yua 12:45:46 #5: 'add real plugin tests using fake ranked+recent
server payloads, run hermes-plugins/tests/run-all.sh. Do not silently
cement the existing 300/400 content truncation: flag DQ-001
separately as still open.'
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musubi import MusubiMemoryProvider  # noqa: E402


def _fake_ranked_payload() -> dict[str, Any]:
    """A representative ranked-mode server payload (per RET-003 spec).

    Top-level mode=fast, results: list[RankedResultRow] (typed).
    Each row has the 9 required fields and the 5-key
    extra.score_components.
    """
    return {
        "mode": "fast",
        "results": [
            {
                "object_id": "3GSGzQauqzXNPstBMJw3hcIV0yd",
                "namespace": "eric/claude-code/episodic",
                "plane": "episodic",
                "score": 0.875,
                "content": "Remember the dentist appointment Tuesday.",
                "state": "matured",
                "importance": 7,
                "score_kind": "ranked_combined",
                "extra": {
                    "score_components": {
                        "relevance": 1.0,
                        "recency": 1.0,
                        "importance": 0.7,
                        "provenance": 0.5,
                        "reinforcement": 0.0,
                    },
                    "lineage": {},
                },
            },
            {
                "object_id": "3GSH1nKs4Ki38WIJo58n6h5km17",
                "namespace": "eric/claude-code/episodic",
                "plane": "episodic",
                "score": 0.65,
                "content": "Eric prefers coffee black.",
                # legacy: state absent (null in wire)
                "state": None,
                "importance": None,
                "score_kind": "ranked_combined",
                "extra": {
                    "score_components": {
                        "relevance": 0.8,
                        "recency": 0.9,
                        "importance": 0.5,  # the internal default normalized
                        "provenance": 0.1,  # 0.1 from scoring._provenance for legacy
                        "reinforcement": 0.0,
                    },
                    "lineage": {},
                },
            },
        ],
        "limit": 5,
        "warnings": [],
    }


def _fake_recent_payload() -> dict[str, Any]:
    """A representative recent-mode server payload (per RET-003 spec).

    Top-level mode=recent, results: list[RecentResultRow] (typed).
    Each row has the 10 required fields, score_kind=created_epoch,
    provenance_score nullable, and extra.score_components exact {}.
    """
    return {
        "mode": "recent",
        "results": [
            {
                "object_id": "3GSGzQauqzXNPstBMJw3hcIV0yd",
                "namespace": "eric/claude-code/episodic",
                "plane": "episodic",
                "score": 1783957804.0,
                "content": "first recent row",
                "state": "matured",
                "importance": 7,
                "score_kind": "created_epoch",
                "provenance_score": 0.5,  # (episodic, matured) in _PROVENANCE
                "extra": {
                    "score_components": {},
                    "lineage": {},
                },
            },
            {
                "object_id": "3GSH1nKs4Ki38WIJo58n6h5km17",
                "namespace": "eric/claude-code/curated",
                "plane": "curated",
                "score": 1783957803.0,
                "content": "second recent row",
                "state": "provisional",
                "importance": 5,
                "score_kind": "created_epoch",
                "provenance_score": None,  # (curated, provisional) NOT in _PROVENANCE
                "extra": {
                    "score_components": {},
                    "lineage": {},
                },
            },
        ],
        "limit": 10,
        "warnings": [],
    }


def _make_provider_with_fake_server(payload: dict) -> MusubiMemoryProvider:
    """Build a MusubiMemoryProvider whose _client.retrieve returns a fixed payload.

    The provider's _tool needs ``_tenant`` and ``_presence`` to build
    the namespace via ``_namespace(plane)``; we set them to the same
    test namespace as the fake server payload (so the recall path
    works end-to-end).
    """
    p = MusubiMemoryProvider.__new__(MusubiMemoryProvider)
    p._client = MagicMock()
    p._client.retrieve = MagicMock(return_value=payload)
    p._tenant = "eric"
    p._presence = "claude-code"
    return p


class TestRankedPassThrough(unittest.TestCase):
    """The plugin transform preserves the new RET-003 fields on ranked rows."""

    def setUp(self) -> None:
        self.p = _make_provider_with_fake_server(_fake_ranked_payload())

    def test_ranked_row_preserves_object_id_score_content(self) -> None:
        """object_id, score, content are preserved (logical KSUID, not Qdrant UUID)."""
        out = self.p._tool("musubi_recall", {"query": "dentist", "limit": 5})
        assert out["ok"] is True
        assert out["status"] == "ok"
        assert out["memories"][0]["object_id"] == "3GSGzQauqzXNPstBMJw3hcIV0yd"
        assert out["memories"][0]["score"] == 0.875
        assert "dentist" in out["memories"][0]["content"]

    def test_ranked_row_preserves_plane_namespace(self) -> None:
        """plane, namespace are preserved (were being STRIPPED before this commit)."""
        out = self.p._tool("musubi_recall", {"query": "dentist", "limit": 5})
        assert out["memories"][0]["plane"] == "episodic"
        assert out["memories"][0]["namespace"] == "eric/claude-code/episodic"

    def test_ranked_row_preserves_state_importance_score_kind(self) -> None:
        """state, importance, score_kind are preserved; nullable for missing legacy."""
        # Row 1: state=matured, importance=7, score_kind=ranked_combined.
        # Row 2: state=None, importance=None (legacy).
        out = self.p._tool("musubi_recall", {"query": "dentist", "limit": 5})
        assert out["memories"][0]["state"] == "matured"
        assert out["memories"][0]["importance"] == 7
        assert out["memories"][0]["score_kind"] == "ranked_combined"
        # Legacy row: null state, null importance.
        assert out["memories"][1]["state"] is None
        assert out["memories"][1]["importance"] is None
        assert out["memories"][1]["score_kind"] == "ranked_combined"

    def test_ranked_row_preserves_extra_score_components_5_keys(self) -> None:
        """extra.score_components is preserved with all 5 public keys verbatim."""
        out = self.p._tool("musubi_recall", {"query": "dentist", "limit": 5})
        # Row 1
        sc = out["memories"][0]["extra"]["score_components"]
        assert set(sc.keys()) == {
            "relevance", "recency", "importance", "provenance", "reinforcement"
        }
        assert sc["relevance"] == 1.0
        assert sc["recency"] == 1.0
        assert sc["importance"] == 0.7
        assert sc["provenance"] == 0.5
        assert sc["reinforcement"] == 0.0


class TestRecentPassThrough(unittest.TestCase):
    """The plugin transform preserves the new RET-003 fields on recent rows."""

    def setUp(self) -> None:
        self.p = _make_provider_with_fake_server(_fake_recent_payload())

    def test_recent_row_preserves_object_id_score(self) -> None:
        """object_id (KSUID), score (created_epoch epoch)."""
        out = self.p._tool("musubi_recall", {"query": "anything", "limit": 5})
        assert out["memories"][0]["object_id"] == "3GSGzQauqzXNPstBMJw3hcIV0yd"
        assert out["memories"][0]["score"] == 1783957804.0

    def test_recent_row_preserves_plane_namespace(self) -> None:
        out = self.p._tool("musubi_recall", {"query": "anything", "limit": 5})
        assert out["memories"][0]["plane"] == "episodic"
        assert out["memories"][0]["namespace"] == "eric/claude-code/episodic"

    def test_recent_row_preserves_state_importance_score_kind(self) -> None:
        out = self.p._tool("musubi_recall", {"query": "anything", "limit": 5})
        assert out["memories"][0]["state"] == "matured"
        assert out["memories"][0]["importance"] == 7
        assert out["memories"][0]["score_kind"] == "created_epoch"

    def test_recent_row_preserves_provenance_score(self) -> None:
        """provenance_score is preserved nullable (0.5 in _PROVENANCE, None if absent pair)."""
        out = self.p._tool("musubi_recall", {"query": "anything", "limit": 5})
        # Row 1: (episodic, matured) → 0.5
        assert out["memories"][0]["provenance_score"] == 0.5
        # Row 2: (curated, provisional) NOT in _PROVENANCE → None
        assert out["memories"][1]["provenance_score"] is None

    def test_recent_row_preserves_extra_score_components_exact_empty(self) -> None:
        """extra.score_components is preserved as the exact empty {} (recent mode)."""
        out = self.p._tool("musubi_recall", {"query": "anything", "limit": 5})
        for m in out["memories"]:
            assert m["extra"]["score_components"] == {}


class TestDQ001TruncationParity(unittest.TestCase):
    """DQ-001: The adapter natively propagates exact string segments and truncation metadata."""

    def test_long_content_is_not_silently_truncated(self) -> None:
        """Load-bearing content is passed completely unaltered."""
        p = _make_provider_with_fake_server(_fake_ranked_payload())
        long_content = "x" * 500
        payload = _fake_ranked_payload()
        payload["results"][0]["content"] = long_content
        p._client.retrieve = MagicMock(return_value=payload)
        out = p._tool("musubi_recall", {"query": "x", "limit": 5})
        actual = out["memories"][0]["content"]
        assert actual == long_content

    def test_truncation_metadata_propagated(self) -> None:
        """Explicit truncation booleans and content length metadata pass through unmodified."""
        p = _make_provider_with_fake_server(_fake_ranked_payload())
        payload = _fake_ranked_payload()
        payload["results"][0]["content_truncated"] = True
        payload["results"][0]["content_length"] = 1500
        p._client.retrieve = MagicMock(return_value=payload)
        out = p._tool("musubi_recall", {"query": "x", "limit": 5})

        m = out["memories"][0]
        assert m.get("content_truncated") is True
        assert m.get("content_length") == 1500

    def test_truncation_metadata_omitted_gracefully_for_legacy_payloads(self) -> None:
        """If legacy payloads omit the boolean/length fields, the adapter must not fabricate them."""
        p = _make_provider_with_fake_server(_fake_ranked_payload())
        payload = _fake_ranked_payload()
        # Ensure they are truly missing
        if "content_truncated" in payload["results"][0]:
            del payload["results"][0]["content_truncated"]
        if "content_length" in payload["results"][0]:
            del payload["results"][0]["content_length"]

        p._client.retrieve = MagicMock(return_value=payload)
        out = p._tool("musubi_recall", {"query": "x", "limit": 5})

        m = out["memories"][0]
        assert "content_truncated" not in m
        assert "content_length" not in m

    def test_truncation_metadata_falsy_values_preserved(self) -> None:
        """Falsy explicit metadata (False, 0) must be preserved exactly, not dropped due to truthiness."""
        p = _make_provider_with_fake_server(_fake_ranked_payload())
        payload = _fake_ranked_payload()
        payload["results"][0]["content_truncated"] = False
        payload["results"][0]["content_length"] = 0
        p._client.retrieve = MagicMock(return_value=payload)

        out = p._tool("musubi_recall", {"query": "x", "limit": 5})
        m = out["memories"][0]

        assert "content_truncated" in m
        assert m["content_truncated"] is False
        assert "content_length" in m
        assert m["content_length"] == 0


if __name__ == "__main__":
    unittest.main()
