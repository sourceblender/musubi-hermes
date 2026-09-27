"""Hermes directory-plugin entry point for Musubi memory."""

from .musubi import MusubiMemoryProvider


def register(ctx) -> None:
    ctx.register_memory_provider(MusubiMemoryProvider())
