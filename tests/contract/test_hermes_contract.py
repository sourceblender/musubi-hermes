"""Musubi provider against the REAL Hermes memory-plugin loader.

Runs inside a hermes-agent checkout's Python (so `agent`, `plugins`,
`hermes_constants` import), never against stubs of them. The plugin under test
is copied into a throwaway HERMES_HOME as a user plugin, exactly where
`hermes plugins install` puts one, and loaded through
`plugins.memory.load_memory_provider("musubi")`.

    MUSUBI_HERMES_PLUGIN_DIR=/path/to/musubi-hermes \
      <hermes-venv>/bin/python -m pytest tests/contract/test_hermes_contract.py

Each test asserts behaviour Hermes documents in
website/docs/developer-guide/memory-provider-plugin.md and plugins/AGENTS.md
(0.21.4): register(ctx) entry, no network in is_available, credentials via
agent.secret_scope.get_secret, background work via spawn_context_thread, state
keyed by the hermes_home handed in, no automatic writes outside primary
contexts, and the contract existing users already depend on (provider name,
`musubi:` config section, data paths, tool names).
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

def _default_plugin_dir() -> Path:
    """The repo root once it is the plugin directory; the legacy musubi/ subdir before."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "__init__.py").is_file() else root / "musubi"


PLUGIN_SRC = Path(os.environ.get("MUSUBI_HERMES_PLUGIN_DIR") or _default_plugin_dir())
CONTRACT_TOOLS = {"musubi_remember", "musubi_recall"}
CONTRACT_CONFIG_KEYS = {"tenant", "presence", "env_file"}
NYLA_GUIDANCE_MARK = "Musubi memories are retrieved on demand."

pytest.importorskip("agent.memory_provider", reason="needs a hermes-agent Python environment")

from agent import secret_scope  # noqa: E402
from agent.memory_provider import MemoryProvider  # noqa: E402


def _copy_plugin(dst: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "*.db", "tests", ".pytest_cache")
    shutil.copytree(PLUGIN_SRC, dst, ignore=ignore)


def _write_config(home: Path, extra: str = "") -> None:
    (home / "config.yaml").write_text(
        "memory:\n  provider: musubi\n"
        "musubi:\n  tenant: contract\n  presence: hermes\n" + extra
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh profile with the plugin installed as a user plugin, and a DECOY
    process HERMES_HOME so any code that reads os.environ instead of the home it
    was handed writes somewhere the assertions can see."""
    h = tmp_path / "profile"
    (h / "plugins").mkdir(parents=True)
    _copy_plugin(h / "plugins" / "musubi")
    _write_config(h)
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    for var in ("MUSUBI_API_URL", "MUSUBI_TOKEN", "MUSUBI_ENV"):
        monkeypatch.delenv(var, raising=False)
    # Drop cached plugin modules so every test imports the copy it installed.
    for mod in [m for m in sys.modules if m.startswith(("plugins.memory.musubi", "_hermes_user_memory", "musubi"))]:
        sys.modules.pop(mod, None)
    yield h, decoy


def _load():
    import plugins.memory as pm
    return pm.load_memory_provider("musubi", register_skills=False)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network touched in a path Hermes requires to be offline")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


# ---- discovery and registration -----------------------------------------------

def test_discovered_as_user_plugin_under_its_contract_name(home):
    import plugins.memory as pm
    names = [n for n, *_ in pm.discover_memory_providers()]
    assert "musubi" in names
    assert pm.find_provider_dir("musubi") == home[0] / "plugins" / "musubi"


def test_ships_manifest_and_registers_through_register_ctx(home):
    manifest = PLUGIN_SRC / "plugin.yaml"
    assert manifest.is_file(), "plugin.yaml is required for catalog install"
    assert "name: musubi" in manifest.read_text()
    import plugins.memory as pm
    mod = pm._load_package(pm.find_provider_dir("musubi"), "musubi")
    assert callable(getattr(mod, "register", None)), (
        "register(ctx) missing: Hermes would fall back to the subclass scan"
    )
    provider = _load()
    assert isinstance(provider, MemoryProvider)
    assert provider.name == "musubi"


# ---- availability ---------------------------------------------------------------

def test_is_available_is_offline_and_false_without_credentials(home, no_network):
    assert _load().is_available() is False


def test_is_available_true_with_scoped_credentials_and_still_offline(home, no_network):
    token = secret_scope.set_secret_scope(
        {"MUSUBI_API_URL": "http://musubi.example.test/v1", "MUSUBI_TOKEN": "t"},
        profile_home=str(home[0]),
    )
    try:
        assert _load().is_available() is True
    finally:
        secret_scope.reset_secret_scope(token)


def test_config_url_and_scoped_token_work_without_legacy_env_file(home, no_network):
    h, _ = home
    _write_config(h, "  api_url: http://musubi.example.test/v1\n")
    token = secret_scope.set_secret_scope({"MUSUBI_TOKEN": "t"}, profile_home=str(h))
    try:
        assert _load().is_available() is True
    finally:
        secret_scope.reset_secret_scope(token)


def test_is_available_does_not_raise_under_multiplex_without_a_scope(home, no_network):
    secret_scope.set_multiplex_active(True)
    try:
        assert _load().is_available() is False
    finally:
        secret_scope.set_multiplex_active(False)


def test_credentials_come_from_the_profile_scope_not_process_env(home, monkeypatch, no_network):
    """Under multiplexing, a credential in os.environ belongs to whichever profile
    launched the process. The provider must see the SCOPED value only."""
    monkeypatch.setenv("MUSUBI_API_URL", "http://wrong-profile.example.test/v1")
    monkeypatch.setenv("MUSUBI_TOKEN", "wrong-profile-token")
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({}, profile_home=str(home[0]))
    try:
        assert _load().is_available() is False
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


# ---- initialize: storage and threads -----------------------------------------------

def _initialize(provider, h: Path, **kw):
    token = secret_scope.set_secret_scope(
        {"MUSUBI_API_URL": "http://musubi.example.test/v1", "MUSUBI_TOKEN": "t"}, profile_home=str(h)
    )
    try:
        provider.initialize("contract-session", hermes_home=str(h), platform="cli", **kw)
    finally:
        secret_scope.reset_secret_scope(token)


def test_storage_follows_the_home_handed_in_not_process_env(home, monkeypatch, no_network):
    h, decoy = home
    # Load while HERMES_HOME is the real profile: discovery legitimately finds user
    # plugins through the process home. Only initialize() must follow the home it is
    # handed, so the decoy goes in between the two.
    provider = _load()
    assert provider is not None
    monkeypatch.setenv("HERMES_HOME", str(decoy))
    _initialize(provider, h, agent_context="primary")
    try:
        assert (h / "musubi-outbox.db").exists(), "outbox must live under the handed-in hermes_home"
        assert not (decoy / "musubi-outbox.db").exists(), "wrote into the process HERMES_HOME instead"
    finally:
        provider.shutdown()


def test_background_worker_is_spawned_with_context(home, monkeypatch, no_network):
    import plugins.memory as pm
    provider = _load()
    mod = sys.modules[type(provider).__module__]
    spawned = []
    real = mod.spawn_context_thread if hasattr(mod, "spawn_context_thread") else None
    assert real is not None, "provider module does not use agent.memory_provider.spawn_context_thread"

    def spy(target, *, name, **kw):
        spawned.append(name)
        return real(target, name=name, **kw)

    monkeypatch.setattr(mod, "spawn_context_thread", spy)
    before = {t.ident for t in threading.enumerate()}
    _initialize(provider, home[0], agent_context="primary")
    try:
        new_musubi = [t for t in threading.enumerate() if t.ident not in before and "musubi" in t.name]
        assert new_musubi, "primary context should start the outbox worker"
        assert len(new_musubi) <= len(spawned), (
            f"musubi threads {[t.name for t in new_musubi]} not all created via spawn_context_thread {spawned}"
        )
    finally:
        provider.shutdown()


@pytest.mark.parametrize("ctx", ["cron", "subagent"])
def test_non_primary_contexts_start_no_worker_and_capture_nothing(home, no_network, ctx):
    provider = _load()
    before = {t.ident for t in threading.enumerate()}
    _initialize(provider, home[0], agent_context=ctx)
    try:
        assert not [t for t in threading.enumerate() if t.ident not in before and "musubi" in t.name]
        provider.sync_turn("user text", "assistant text", session_id="contract-session")
        health = provider._outbox.health() if getattr(provider, "_outbox", None) else {}
        assert not health.get("pending"), f"{ctx} context captured a turn: {health}"
    finally:
        provider.shutdown()


# ---- the contract existing users depend on -----------------------------------------

def test_tool_names_are_unchanged(home):
    names = {s["name"] for s in _load().get_tool_schemas()}
    assert CONTRACT_TOOLS <= names, f"missing contract tools: {CONTRACT_TOOLS - names}"


def test_config_schema_keeps_existing_keys_and_adds_recall_guidance(home):
    keys = {f["key"] for f in _load().get_config_schema()}
    assert CONTRACT_CONFIG_KEYS <= keys
    assert "recall_guidance" in keys


def test_recall_guidance_defaults_to_nylas_text_and_is_configurable(home, no_network):
    h, _ = home
    provider = _load()
    _initialize(provider, h, agent_context="primary")
    try:
        block = provider.system_prompt_block()
        assert NYLA_GUIDANCE_MARK in block
        assert "Eric" not in block
    finally:
        provider.shutdown()

    _write_config(h, "  recall_guidance: SUMI-SPECIFIC GUIDANCE MARKER\n")
    provider = _load()
    _initialize(provider, h, agent_context="primary")
    try:
        block = provider.system_prompt_block()
        assert "SUMI-SPECIFIC GUIDANCE MARKER" in block
        assert NYLA_GUIDANCE_MARK not in block
        assert "durable long-term memory" in block
        assert "musubi_remember" in block
        assert "only real once it is verified" in block
        assert "queued" in block and "FAILED" in block
    finally:
        provider.shutdown()


def test_save_config_round_trips_non_secret_fields(home, no_network):
    h, _ = home
    (h / "config.yaml").write_text("memory:\n  provider: musubi\n")
    _load().save_config({"tenant": "roundtrip", "presence": "hermes",
                         "env_file": "/nonexistent", "api_url": "https://musubi.example.test/v1"}, str(h))
    text = (h / "config.yaml").read_text()
    assert "roundtrip" in text and "musubi:" in text
    assert "musubi.example.test" in text
    assert "MUSUBI_TOKEN" not in text
    assert "memory:" in text, "save_config must merge, not overwrite, config.yaml"
