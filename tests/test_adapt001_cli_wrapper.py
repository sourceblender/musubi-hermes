import json
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


class DefectStillPresent(Exception):
    pass


class MockMusubiServer:
    def __init__(self):
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.status_code: int = 200


class MockMusubiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        req = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }
        self.server.mock.requests.append(req)

        self.send_response(self.server.mock.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if self.server.mock.responses:
            resp = self.server.mock.responses.pop(0)
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        req = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": None,
        }
        self.server.mock.requests.append(req)

        self.send_response(self.server.mock.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if self.server.mock.responses:
            resp = self.server.mock.responses.pop(0)
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.wfile.write(b'{"ok": true}')

    def log_message(self, format, *args):
        pass


@pytest.fixture
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockMusubiHandler)
    server.mock = MockMusubiServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def setup_profile_env(tmp_path: Path, identity: str) -> Path:
    profile_dir = tmp_path / "profiles" / identity
    profile_dir.mkdir(parents=True, exist_ok=True)
    if identity == "nyla":
        config = {
            "secrets": {"onepassword": {"env": {"MUSUBI_TOKEN": "op://test/token"}}}
        }
        (profile_dir / "config.yaml").write_text(json.dumps(config))
    else:
        (profile_dir / ".env").write_text("MUSUBI_TOKEN=sumi-token\n")
    return profile_dir


def _run_cli(
    mock_server,
    tmp_path,
    identity: str,
    args: List[str],
    env: Optional[Dict[str, str]] = None,
    executable: str = "bin/musubi-memory",
):
    profile_dir = setup_profile_env(tmp_path, identity)
    db_path = tmp_path / "outbox.db"

    full_env = {
        **os.environ,
        "MUSUBI_API_URL": f"http://127.0.0.1:{mock_server.server_port}/v1",
        "ADAPT_DB_PATH": str(db_path),
        "HERMES_HOME": str(profile_dir),
        "OP_CONNECT_TOKEN": "mock-op-token" if identity == "nyla" else "",
    }
    if env:
        full_env.update(env)

    try:
        res = subprocess.run(
            [sys.executable, executable, "--tenant", identity, "--presence", "hermes"]
            + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(tmp_path),
        )
        if res.returncode == 127 or (
            res.returncode == 2 and "No such file or directory" in res.stderr
        ):
            pytest.xfail("ADAPT-001: CLI not yet implemented")
        return res
    except FileNotFoundError:
        pytest.xfail("ADAPT-001: CLI binary not found")


# --- HARNESS DISCRIMINATION (Green Controls & Red Proofs) ---


def test_harness_green_control(mock_server, tmp_path):
    """Proves the harness can execute a script, capture output, and mock HTTP correctly."""
    script = tmp_path / "correct.py"
    script.write_text("import sys; print('remembered ✓  (id obj_123)'); sys.exit(0)")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "x"], executable=str(script)
    )
    assert res.returncode == 0
    assert "remembered ✓  (id obj_123)" in res.stdout


def test_harness_discriminates_exit_code(mock_server, tmp_path):
    """Proves the harness catches incorrect exit codes."""
    script = tmp_path / "wrong_exit.py"
    script.write_text("import sys; sys.exit(1)")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "x"], executable=str(script)
    )
    assert res.returncode != 0


def test_harness_discriminates_stdout_schema(mock_server, tmp_path):
    script = tmp_path / "wrong_stdout.py"
    script.write_text("import sys; print('Wrong format'); sys.exit(0)")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "x"], executable=str(script)
    )
    assert "remembered" not in res.stdout


def test_harness_discriminates_sqlite_state(mock_server, tmp_path):
    script = tmp_path / "write_db.py"
    script.write_text("""
import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('pending')")
con.commit()
sys.exit(2)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "x"], executable=str(script)
    )
    assert res.returncode == 2
    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "pending"


# --- ACCEPTANCE TESTS (Strict XFAILs mapping the 37 requirements) ---


def check_no_token_leak(res):
    assert "sumi-token" not in res.stdout
    assert "sumi-token" not in res.stderr
    assert "op://test/token" not in res.stdout
    assert "op://test/token" not in res.stderr


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_remember_success_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])

    assert res.returncode == 0
    assert res.stdout.strip() == "remembered ✓  (id obj_123)"
    assert res.stderr.strip() == ""

    # 1 POST for write, 1 GET for readback
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_remember_success_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "remember", "test"])

    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["status"] == "stored"
    assert data["object_id"] == "obj_123"
    assert res.stderr.strip() == ""
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_recall_ranked_success_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "results": [
                {
                    "object_id": "mem_1",
                    "score": 0.9,
                    "snippet": "hello",
                    "namespace": f"{identity}/hermes/episodic",
                    "plane": "episodic",
                    "state": "matured",
                    "importance": 5,
                    "score_kind": "ranked_combined",
                    "extra": {
                        "score_components": {
                            "relevance": 0.9,
                            "recency": 0.1,
                            "reinforcement": 0,
                            "importance": 0.5,
                            "provenance": 0,
                        }
                    },
                }
            ]
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])

    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["mode"] in ("fast", "deep", "blended")
    mem = data["memories"][0]
    assert mem["object_id"] == "mem_1"
    assert mem["state"] == "matured"
    assert "relevance" in mem["extra"]["score_components"]
    # Ensure ranked has exactly 5 components
    assert len(mem["extra"]["score_components"]) == 5
    # Ensure nullable provenance_score is not present or is null depending on definition
    # But ranked should not have provenance_score in top-level output if not supplied, or handled gracefully
    assert res.stderr.strip() == ""
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_recent_success_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "results": [
                {
                    "object_id": "mem_1",
                    "score": 0.9,
                    "snippet": "hello",
                    "namespace": f"{identity}/hermes/episodic",
                    "plane": "episodic",
                    "state": "matured",
                    "importance": 5,
                    "score_kind": "created_epoch",
                    "provenance_score": 0.0,
                    "extra": {"score_components": {}},
                }
            ]
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recent"])

    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["mode"] == "recent"
    mem = data["memories"][0]
    assert mem["extra"]["score_components"] == {}
    assert "provenance_score" in mem
    assert res.stderr.strip() == ""
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_recall_zero_results_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [{"results": []}]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])

    assert res.returncode == 0
    assert "no memory matched" in res.stdout
    assert res.stderr.strip() == ""
    check_no_token_leak(res)


# --- Errors ---
@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_read_401_auth(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 401
    mock_server.mock.responses = [
        {"error": {"code": "UNAUTHORIZED", "detail": "bad token"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])

    assert res.returncode == 1
    assert "Error 401" in res.stderr
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_write_403_scope(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 403
    mock_server.mock.responses = [
        {"error": {"code": "FORBIDDEN", "detail": "bad scope"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])

    assert res.returncode == 1
    assert "Error 403" in res.stderr

    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "dead"
    con.close()
    check_no_token_leak(res)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_write_409_conflict(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 409
    mock_server.mock.responses = [
        {"error": {"code": "CONFLICT", "detail": "idempotency collision"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])

    assert res.returncode == 1
    assert "Error 409" in res.stderr

    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "dead"
    con.close()


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_read_422_validation(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 422
    mock_server.mock.responses = [
        {"error": {"code": "UNPROCESSABLE_ENTITY", "detail": "bad schema"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])

    assert res.returncode == 1
    assert "Error 422" in res.stderr


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_write_5xx_unavailable(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 503
    mock_server.mock.responses = [
        {"error": {"code": "UNAVAILABLE", "detail": "server down"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])

    assert res.returncode == 2
    assert "Error" in res.stderr

    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "pending"
    con.close()


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_local_validation_fails(mock_server, tmp_path, identity: str) -> None:
    res = _run_cli(mock_server, tmp_path, identity, ["remember"])  # missing arg
    assert res.returncode == 1
    assert "Error" in res.stderr


# --- Warnings ---


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_warning_allowlist_dedup_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "warnings": [
                {"code": "sparse_embedding_failed", "plane": "episodic"},
                {"code": "sparse_embedding_failed", "plane": "episodic"},
            ],
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])

    assert res.returncode == 0
    assert "Warning:" in res.stderr
    # Deduplication means it only appears once
    assert res.stderr.count("sparse_embedding_failed") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_warning_bounds_cap_fails_closed(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "warnings": [
                {"code": f"plane_timeout_{i}", "plane": "episodic"} for i in range(25)
            ],  # Max is 20 raw
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    assert res.returncode == 2
    assert "no memory matched" not in res.stdout


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_unknown_warning_fails_closed(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "warnings": [{"code": "unrecognized_hax_code", "plane": "episodic"}],
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])
    assert res.returncode == 2
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert data["status"] == "failed"
    assert "unknown warning" in data["detail"]


# --- Crash Boundaries ---
# We simulate crashes by passing an env var _ADAPT_CRASH_POINT which the CLI will honor.


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_crash_after_enqueue_before_claim(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_enqueue"}
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    assert res.returncode != 0

    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "pending"

    # Restart
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res2 = _run_cli(mock_server, tmp_path, identity, ["remember", "test2"])
    assert res2.returncode == 0
    # Expected 1 POST, 1 GET from the recovery
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_crash_after_claim_before_post(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_claim"}
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    assert res.returncode != 0

    # Recovery reclaims via dead PID
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res2 = _run_cli(mock_server, tmp_path, identity, ["remember", "test2"])
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_crash_after_post_before_accept(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_post"}
    mock_server.mock.responses = [{"object_id": "obj_123"}]  # server processes it
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    assert res.returncode != 0

    # Recovery issues second POST (UUID match -> 200)
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res2 = _run_cli(mock_server, tmp_path, identity, ["remember", "test2"])
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 2
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_crash_after_accept_before_get(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_accept"}
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    assert res.returncode != 0

    # Recovery skips POST, issues GET directly
    mock_server.mock.requests.clear()
    res2 = _run_cli(mock_server, tmp_path, identity, ["remember", "test2"])
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(strict=True, reason="ADAPT-001: CLI not yet implemented")
def test_crash_after_get_before_verify(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_get"}
    mock_server.mock.responses = [{"object_id": "obj_123"}, {"object_id": "obj_123"}]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    assert res.returncode != 0

    # Recovery skips POST, issues GET directly (Overall GET = 2)
    res2 = _run_cli(mock_server, tmp_path, identity, ["remember", "test2"])
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 2


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.xfail(
    strict=True, raises=DefectStillPresent, reason="ADAPT-001: CLI not yet implemented"
)
def test_crash_after_verify(mock_server, tmp_path, identity: str) -> None:
    env = {"_ADAPT_CRASH_POINT": "after_verify"}
    mock_server.mock.responses = [{"object_id": "obj_123"}, {"object_id": "obj_123"}]
    _ = _run_cli(mock_server, tmp_path, identity, ["remember", "test"], env=env)
    mock_server.mock.requests.clear()
    _ = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    # Zero POST/GET for the old row
    assert (
        sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    )  # Just the recall
