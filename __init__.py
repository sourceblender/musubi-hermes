"""Hermes directory-plugin entry point for Musubi memory."""

if __package__:
    from .musubi import MusubiMemoryProvider
else:  # pytest imports this repository root as a plain module.
    from musubi import MusubiMemoryProvider


def register(ctx) -> None:
    ctx.register_memory_provider(MusubiMemoryProvider())
