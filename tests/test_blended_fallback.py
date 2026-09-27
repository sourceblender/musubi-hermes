"""RET-015 client contract: one blended attempt, one fast fallback, never retry blended.

The server repair (musubi v1.23.6) removed the 503 cliff at ten concurrent callers, but
the whole-call budget is still ~90% consumed at that concurrency and musubi#681 owns the
next ceiling. This is the client's defence in depth — and the reason it must NOT retry
blended is that a retry adds another blocking call to the path that is already starving.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "musubi" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermes_musubi_under_test", _PLUGIN)
assert _spec and _spec.loader
hm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hm)


class _FakeClient:
    """Records every retrieve call so the test can assert what was NOT sent."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[str] = []

    def retrieve(self, namespace, *, mode, limit, query_text, **_):
        self.calls.append(mode)
        outcome = self.behaviour(mode)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _provider(client):
    p = object.__new__(hm.MusubiMemoryProvider)
    p._client = client  # noqa: SLF001 — constructing the seam under test directly
    return p


def _err(status):
    return hm.MusubiError(f"HTTP {status}", status=status, retryable=status != 401)


class BlendedFallbackContract(unittest.TestCase):
    def test_healthy_blended_never_calls_fast(self):
        c = _FakeClient(lambda mode: {"results": [{"score": 0.9}]})
        payload, mode, fell_back = _provider(c)._retrieve_with_fallback(
            "n/h/episodic", limit=5, query_text="q"
        )
        self.assertEqual(c.calls, ["blended"])
        self.assertEqual(mode, "blended")
        self.assertFalse(fell_back)
        self.assertTrue(payload["results"])

    def test_503_falls_back_to_fast_exactly_once_and_never_retries_blended(self):
        def behaviour(mode):
            if mode == "blended":
                return _err(503)
            return {"results": [{"score": 0.7}]}

        c = _FakeClient(behaviour)
        payload, mode, fell_back = _provider(c)._retrieve_with_fallback(
            "n/h/episodic", limit=5, query_text="q"
        )
        # The whole contract, asserted as an exact sequence: one blended, one fast.
        self.assertEqual(c.calls, ["blended", "fast"])
        self.assertEqual(c.calls.count("blended"), 1, "a second blended request feeds the outage")
        self.assertEqual(mode, "fast")
        self.assertTrue(fell_back)
        self.assertTrue(payload["results"])

    def test_non_503_errors_raise_and_never_reach_fast(self):
        # A 401 is not a capacity problem. Re-asking in a cheaper mode would hide it.
        for status in (400, 401, 403, 404, 422, 500):
            with self.subTest(status=status):
                c = _FakeClient(lambda mode, s=status: _err(s))
                with self.assertRaises(hm.MusubiError):
                    _provider(c)._retrieve_with_fallback("n/h/episodic", limit=5, query_text="q")
                self.assertEqual(c.calls, ["blended"], "only 503 may trigger the fallback")

    def test_fast_failure_after_503_raises_rather_than_faking_an_empty_recall(self):
        # Two dead modes is an outage. Returning [] would read to the model as
        # "I have no memories about that", which is a lie with the same shape as an answer.
        c = _FakeClient(lambda mode: _err(503))
        with self.assertRaises(hm.MusubiError):
            _provider(c)._retrieve_with_fallback("n/h/episodic", limit=5, query_text="q")
        self.assertEqual(c.calls, ["blended", "fast"])

    def test_fallback_mode_is_a_real_musubi_mode(self):
        # "search" 422s on this API; the valid set is fast/deep/blended/recent.
        self.assertIn(hm.MODE_FALLBACK, {"fast", "deep", "blended", "recent"})
        self.assertNotEqual(hm.MODE_FALLBACK, hm.MODE_QUERY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
