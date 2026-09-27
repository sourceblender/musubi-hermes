"""Hermes capture hygiene: keep runtime routing syntax out of durable memory."""

from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from musubi import MusubiMemoryProvider  # noqa: E402


class MemoryHygieneTests(unittest.TestCase):
    def capture(self, user: str, assistant: str) -> str:
        provider = object.__new__(MusubiMemoryProvider)
        provider._session_id = "test-session"  # noqa: SLF001
        captured: list[str] = []

        def enqueue(content: str, **_kwargs: Any) -> int:
            captured.append(content)
            return 1

        provider._enqueue = enqueue  # type: ignore[method-assign]  # noqa: SLF001
        provider.sync_turn(user, assistant, session_id="test-session")
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_trigger_bracket_is_removed_from_user_memory(self) -> None:
        content = self.capture(
            "[Triggering message id: `1537109419354226729` — use as `message_id` for "
            "reply/react/pin via the discord tools.]\n\nTell me what you found.",
            "I found it.",
        )

        self.assertEqual(content, "USER: Tell me what you found.\n\nASSISTANT: I found it.")

    def test_trigger_bracket_without_backticks_is_removed(self) -> None:
        content = self.capture(
            "[Triggering message id: 1537109419354226729 — use as `message_id` for "
            "reply/react/pin via the discord tools.] Next thought.",
            "Kept.",
        )

        self.assertEqual(content, "USER: Next thought.\n\nASSISTANT: Kept.")

    def test_media_paths_become_picture_descriptions(self) -> None:
        content = self.capture(
            "Look.",
            "MEDIA:/tmp/example.png and "
            "MEDIA:/tmp/second.webp",
        )

        self.assertEqual(
            content,
            "USER: Look.\n\nASSISTANT: [sent a picture: example.png] and "
            "[sent a picture: second.webp]",
        )

    def test_unrelated_brackets_and_urls_are_preserved(self) -> None:
        content = self.capture(
            "[keep this] https://example.com/file.png",
            "ordinary response",
        )

        self.assertEqual(
            content,
            "USER: [keep this] https://example.com/file.png\n\nASSISTANT: ordinary response",
        )

    def test_fully_scrubbed_turn_is_not_enqueued(self) -> None:
        provider = object.__new__(MusubiMemoryProvider)
        provider._session_id = "test-session"  # noqa: SLF001
        captured: list[str] = []
        provider._enqueue = lambda content, **kwargs: captured.append(content)  # type: ignore[method-assign]  # noqa: SLF001,E501

        provider.sync_turn(
            "[Triggering message id: `1537109419354226729` — use as `message_id` for "
            "reply/react/pin via the discord tools.]",
            "",
            session_id="test-session",
        )

        self.assertEqual(captured, [])


if __name__ == "__main__":
    unittest.main()
