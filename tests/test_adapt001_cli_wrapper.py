import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
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
        self.malformed_json: bool = False
        self.timeout_delay: float = 0.0


class MockMusubiHandler(BaseHTTPRequestHandler):
    def _read_req(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        return {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body) if body else None,
        }

    def _write_resp(self):
        if self.server.mock.timeout_delay > 0:
            time.sleep(self.server.mock.timeout_delay)

        self.send_response(self.server.mock.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if self.server.mock.malformed_json:
            self.wfile.write(b'{malformed: json"')
            return

        if self.server.mock.responses:
            resp = self.server.mock.responses.pop(0)
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.wfile.write(b'{"ok": true}')

    def do_POST(self):
        self.server.mock.requests.append(self._read_req())
        self._write_resp()

    def do_GET(self):
        self.server.mock.requests.append(self._read_req())
        self._write_resp()

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


def _resolved_token(identity: str) -> str:
    return f"{identity}-resolved-token"


def _run_cli(
    mock_server,
    tmp_path,
    identity: str,
    args: List[str],
    env: Optional[Dict[str, str]] = None,
    executable: Optional[str] = None,
    operation_id: Optional[str] = None,
):
    profile_dir = setup_profile_env(tmp_path, identity)
    db_path = tmp_path / "outbox.db"

    repo_root = Path(__file__).resolve().parent.parent.parent
    abs_executable = (
        str(repo_root / executable)
        if executable
        else str(repo_root / "bin/musubi-memory")
    )

    full_env = {
        **os.environ,
        "MUSUBI_API_URL": f"http://127.0.0.1:{mock_server.server_port}/v1",
        "MUSUBI_TOKEN": _resolved_token(identity),
        "ADAPT_DB_PATH": str(db_path),
        "HERMES_HOME": str(profile_dir),
    }
    if env:
        full_env.update(env)

    command_args = list(args)
    if "remember" in command_args and "--operation-id" not in command_args:
        command_args.extend(
            ["--operation-id", operation_id or f"test-{uuid.uuid4().hex}"]
        )

    try:
        res = subprocess.run(
            [
                sys.executable,
                abs_executable,
                "--tenant",
                identity,
                "--presence",
                "hermes",
            ]
            + command_args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(tmp_path),
            timeout=5.0,
        )
        if executable is None:
            if (
                res.returncode == 127
                or (res.returncode == 2 and "No such file or directory" in res.stderr)
                or (res.returncode == 1 and "can't open file" in res.stderr)
            ):
                raise DefectStillPresent("ADAPT-001: CLI not yet implemented")
        return res
    except FileNotFoundError:
        if executable is None:
            raise DefectStillPresent("ADAPT-001: CLI binary not found")
        raise


# -----------------------------------------------------------------------------
# Core Validators
# -----------------------------------------------------------------------------


def _assert_no_token_leak(res, mock_server, tmp_path, identity):
    token = _resolved_token(identity)
    configured = "op://test/token" if identity == "nyla" else "sumi-token"
    # Ensure it's not in stdout/stderr
    for secret in (token, configured):
        assert secret not in res.stdout, f"Token leaked in stdout for {identity}"
        assert secret not in res.stderr, f"Token leaked in stderr for {identity}"

    # Check requests to ensure it ONLY appears in the Authorization header
    for req in mock_server.mock.requests:
        assert req["headers"].get("Authorization") == f"Bearer {token}", (
            f"Token missing or malformed in Auth header for {identity}"
        )
        # Serialize the rest of the request to check for leaks
        req_copy = dict(req)
        del req_copy["headers"]["Authorization"]
        assert token not in json.dumps(req_copy), (
            f"Token leaked in request body/path for {identity}"
        )

    # Check database
    db_path = tmp_path / "outbox.db"
    if db_path.exists():
        with open(db_path, "rb") as f:
            content = f.read().decode("utf-8", errors="ignore")
            for secret in (token, configured):
                assert secret not in content, (
                    f"Token leaked in SQLite outbox for {identity}"
                )


def _assert_remember_success_prose(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    assert res.stdout.strip() == "remembered ✓  (id obj_123)"
    assert res.stderr.strip() == ""
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 1
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1

    post_req = next(r for r in mock_server.mock.requests if r["method"] == "POST")
    assert "Idempotency-Key" in post_req["headers"]
    assert post_req["path"] == "/v1/episodic"
    assert post_req["body"]["namespace"] == f"{identity}/hermes/episodic"
    assert post_req["body"]["content"] == "test"
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_remember_success_json(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["status"] == "stored"
    assert data["object_id"] == "obj_123"
    assert res.stderr.strip() == ""

    post_req = next(r for r in mock_server.mock.requests if r["method"] == "POST")
    assert "Idempotency-Key" in post_req["headers"]
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_recall_ranked_success_json(res, mock_server, tmp_path, identity, mode):
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["mode"] == mode
    assert data["limit"] == 5
    assert data["warnings"] == []
    mem = data["memories"][0]
    assert mem["object_id"] == "mem_1"
    assert mem["namespace"] == f"{identity}/hermes/episodic"
    assert mem["plane"] == "episodic"
    assert mem["content"] == "hello"
    assert mem["state"] == "matured"
    assert mem["importance"] == 5
    assert mem["score_kind"] == "ranked_combined"
    assert set(mem["extra"]["score_components"]) == {
        "relevance",
        "recency",
        "reinforcement",
        "importance",
        "provenance",
    }
    assert res.stderr.strip() == ""

    post_req = next(r for r in mock_server.mock.requests if r["method"] == "POST")
    assert post_req["path"] == "/v1/retrieve"
    assert post_req["body"]["state_filter"] == ["provisional", "matured", "promoted"]
    assert post_req["body"]["query_text"] == "test"
    assert post_req["body"]["namespace"] == f"{identity}/hermes"
    assert post_req["body"]["planes"] == ["episodic"]
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_recent_success_json(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["mode"] == "recent"
    assert data["limit"] == 5
    assert data["warnings"] == []
    mem = data["memories"][0]
    assert mem["object_id"] == "mem_1"
    assert mem["namespace"] == f"{identity}/hermes/episodic"
    assert mem["plane"] == "episodic"
    assert mem["content"] == "hello"
    assert mem["state"] == "matured"
    assert mem["importance"] == 5
    assert mem["score_kind"] == "created_epoch"
    assert mem["extra"]["score_components"] == {}
    assert mem["provenance_score"] == 0.0
    assert res.stderr.strip() == ""

    post_req = next(r for r in mock_server.mock.requests if r["method"] == "POST")
    assert post_req["body"]["mode"] == "recent"
    assert post_req["body"]["namespace"] == f"{identity}/hermes"
    assert post_req["body"]["planes"] == ["episodic"]
    assert "state_filter" not in post_req["body"]
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_recall_zero_results_prose(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    assert "no memory matched 'test' yet." in res.stdout
    assert res.stderr.strip() == ""
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_nullable_metadata_preserved_json(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    data = json.loads(res.stdout)
    mem = data["memories"][0]
    assert mem["state"] is None
    assert mem["importance"] is None


def _assert_read_401_auth(res, mock_server, tmp_path, identity):
    assert res.returncode == 1
    assert "Error 401" in res.stderr
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_write_403_scope(res, mock_server, tmp_path, identity):
    assert res.returncode == 1
    assert "Error 403" in res.stderr
    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "dead"
    con.close()
    _assert_no_token_leak(res, mock_server, tmp_path, identity)


def _assert_write_409_conflict(res, mock_server, tmp_path, identity):
    assert res.returncode == 1
    assert "Error 409" in res.stderr
    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "dead"
    con.close()


def _assert_read_422_validation(res, mock_server, tmp_path, identity):
    assert res.returncode == 1
    assert "Error 422" in res.stderr


def _assert_write_5xx_unavailable(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    assert "Error" in res.stderr
    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "pending"
    con.close()


def _assert_write_timeout(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    assert "Error" in res.stderr
    con = sqlite3.connect(tmp_path / "outbox.db")
    state = con.execute("SELECT state FROM outbox").fetchone()[0]
    assert state == "pending"
    con.close()


def _assert_read_malformed_json(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    assert "Error" in res.stderr
    assert "ok" not in res.stdout


def _assert_write_local_enqueue_crash(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    assert "Error" in res.stderr
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(tmp_path / "outbox.db").execute("SELECT * FROM outbox")


def _assert_warning_allowlist_dedup_prose(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    assert "Warning: [sparse_embedding_failed]" in res.stderr
    assert "Warning: [reranker_failed]" in res.stderr
    assert res.stderr.count("sparse_embedding_failed") == 1


def _assert_warning_allowlist_dedup_json(res, mock_server, tmp_path, identity):
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert len(data["warnings"]) == 1
    assert data["warnings"][0] == "sparse_embedding_failed"
    assert res.stderr.strip() == ""


def _assert_warning_bounds_cap_fails_closed(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    assert "no memory matched" not in res.stdout


def _assert_unknown_warning_fails_closed(res, mock_server, tmp_path, identity):
    assert res.returncode == 2
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert data["status"] == "failed"
    assert "unknown warning" in data["detail"]


# -----------------------------------------------------------------------------
# Layer A: Harness Discriminators (Green Controls & Red Proofs)
# -----------------------------------------------------------------------------


def test_harness_green_remember_prose(mock_server, tmp_path):
    script = tmp_path / "correct.py"
    script.write_text("""import sys, os, urllib.request, json
token = os.environ["MUSUBI_TOKEN"]
req = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic", data=json.dumps({"namespace": sys.argv[2] + "/hermes/episodic", "content": "test"}).encode(), headers={"Idempotency-Key": "123", "Authorization": f"Bearer {token}"}, method="POST")
urllib.request.urlopen(req)
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
print("remembered ✓  (id obj_123)")
sys.exit(0)
""")
    mock_server.mock.responses = [{"object_id": "obj_123"}, {"object_id": "obj_123"}]
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "test"], executable=str(script)
    )
    _assert_remember_success_prose(res, mock_server, tmp_path, "nyla")


def test_harness_red_remember_prose_drops_idempotency(mock_server, tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text("""import sys, os, urllib.request, json
token = os.environ["MUSUBI_TOKEN"]
req = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic", data=json.dumps({"namespace": sys.argv[2] + "/hermes/episodic", "content": "test"}).encode(), headers={"Authorization": f"Bearer {token}"}, method="POST")
urllib.request.urlopen(req)
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
print("remembered ✓  (id obj_123)")
sys.exit(0)
""")
    mock_server.mock.responses = [{"object_id": "obj_123"}, {"object_id": "obj_123"}]
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "test"], executable=str(script)
    )
    with pytest.raises(AssertionError):  # Will fail on Idempotency-Key
        _assert_remember_success_prose(res, mock_server, tmp_path, "nyla")


def test_harness_green_read_401(mock_server, tmp_path):
    script = tmp_path / "correct.py"
    script.write_text("""import sys
sys.stderr.write("Error 401: Unauthorized")
sys.exit(1)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["recall", "test"], executable=str(script)
    )
    _assert_read_401_auth(res, mock_server, tmp_path, "nyla")


def test_harness_red_read_401_exits_0(mock_server, tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text("""import sys
sys.stderr.write("Error 401: Unauthorized")
sys.exit(0)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["recall", "test"], executable=str(script)
    )
    with pytest.raises(AssertionError):
        _assert_read_401_auth(res, mock_server, tmp_path, "nyla")


def test_harness_red_token_leak_stdout(mock_server, tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text("""import sys
print("op://test/token")
sys.exit(0)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "test"], executable=str(script)
    )
    with pytest.raises(AssertionError, match="Token leaked in stdout"):
        _assert_no_token_leak(res, mock_server, tmp_path, "nyla")


def test_harness_green_write_timeout(mock_server, tmp_path):
    script = tmp_path / "correct.py"
    script.write_text("""import sys, sqlite3, os, urllib.request, urllib.error
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('pending')")
con.commit()
sys.stderr.write("Error: network timeout")
sys.exit(2)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "test"], executable=str(script)
    )
    _assert_write_timeout(res, mock_server, tmp_path, "nyla")


def test_harness_red_write_timeout_fails_open(mock_server, tmp_path):
    script = tmp_path / "wrong.py"
    script.write_text("""import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('dead')")
con.commit()
sys.stderr.write("Error: network timeout")
sys.exit(2)
""")
    res = _run_cli(
        mock_server, tmp_path, "nyla", ["remember", "test"], executable=str(script)
    )
    with pytest.raises(AssertionError):  # state != pending
        _assert_write_timeout(res, mock_server, tmp_path, "nyla")


# -----------------------------------------------------------------------------
# Layer B: Production Target Assertions (Strict XFAILs raising DefectStillPresent)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_remember_success_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {"object_id": "obj_123"},
        {
            "object_id": "obj_123",
            "namespace": f"{identity}/hermes/episodic",
            "content": "test",
        },
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])
    _assert_remember_success_prose(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_remember_success_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {"object_id": "obj_123"},
        {
            "object_id": "obj_123",
            "namespace": f"{identity}/hermes/episodic",
            "content": "test",
        },
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "remember", "test"])
    _assert_remember_success_json(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_two_intentional_identical_remembers_create_distinct_memories(
    mock_server, tmp_path, identity: str
) -> None:
    namespace = f"{identity}/hermes/episodic"
    mock_server.mock.responses = [
        {"object_id": "obj_first"},
        {"object_id": "obj_first", "namespace": namespace, "content": "same"},
        {"object_id": "obj_second"},
        {"object_id": "obj_second", "namespace": namespace, "content": "same"},
    ]

    first = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["--json", "remember", "same"],
        operation_id="intent-first",
    )
    second = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["--json", "remember", "same"],
        operation_id="intent-second",
    )

    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)["object_id"] == "obj_first"
    assert json.loads(second.stdout)["object_id"] == "obj_second"
    episodic_posts = [
        request
        for request in mock_server.mock.requests
        if request["method"] == "POST" and request["path"] == "/v1/episodic"
    ]
    assert len(episodic_posts) == 2
    assert len({request["headers"]["Idempotency-Key"] for request in episodic_posts}) == 2
    with sqlite3.connect(tmp_path / "outbox.db") as con:
        rows = con.execute(
            "SELECT state, object_id, idem_key FROM outbox ORDER BY id"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("verified", "obj_first"),
        ("verified", "obj_second"),
    ]
    assert rows[0][2] != rows[1][2]
    _assert_no_token_leak(second, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_operation_id_reuse_with_different_content_fails_closed(
    mock_server, tmp_path, identity: str
) -> None:
    namespace = f"{identity}/hermes/episodic"
    mock_server.mock.responses = [
        {"object_id": "obj_first"},
        {"object_id": "obj_first", "namespace": namespace, "content": "first"},
    ]
    first = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "first"],
        operation_id="same-operation",
    )
    second = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "different"],
        operation_id="same-operation",
    )

    assert first.returncode == 0
    assert second.returncode == 2
    assert "operation identity was reused" in second.stderr
    assert sum(request["method"] == "POST" for request in mock_server.mock.requests) == 1
    assert sum(request["method"] == "GET" for request in mock_server.mock.requests) == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
@pytest.mark.parametrize("mode", ["fast", "deep", "blended"])
def test_recall_ranked_success_json(
    mock_server, tmp_path, identity: str, mode: str
) -> None:
    mock_server.mock.responses = [
        {
            "mode": mode,
            "limit": 5,
            "warnings": [],
            "results": [
                {
                    "object_id": "mem_1",
                    "score": 0.9,
                    "content": "hello",
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
            ],
        }
    ]
    args = ["--json", "recall", "test"]
    if mode != "deep":
        args.extend(["--mode", mode])
    res = _run_cli(mock_server, tmp_path, identity, args)
    _assert_recall_ranked_success_json(res, mock_server, tmp_path, identity, mode)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_recent_success_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "recent",
            "limit": 5,
            "warnings": [],
            "results": [
                {
                    "object_id": "mem_1",
                    "score": 0.9,
                    "content": "hello",
                    "namespace": f"{identity}/hermes/episodic",
                    "plane": "episodic",
                    "state": "matured",
                    "importance": 5,
                    "score_kind": "created_epoch",
                    "provenance_score": 0.0,
                    "extra": {"score_components": {}},
                }
            ],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recent"])
    _assert_recent_success_json(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_recall_preserves_full_content_without_adapter_truncation(
    mock_server, tmp_path, identity: str
) -> None:
    content_str = "prefix-" + ("memory-evidence-" * 80) + "-decisive-suffix"
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": [],
            "results": [
                {
                    "object_id": "mem_long",
                    "score": 0.9,
                    "content": content_str,
                    "content_truncated": True,
                    "content_length": 1500,
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
                },
                {
                    "object_id": "mem_legacy",
                    "score": 0.8,
                    "content": "legacy row without metadata",
                    "namespace": f"{identity}/hermes/episodic",
                    "plane": "episodic",
                    "state": "matured",
                    "importance": 5,
                    "score_kind": "ranked_combined",
                    "extra": {
                        "score_components": {
                            "relevance": 0.8,
                            "recency": 0.1,
                            "reinforcement": 0,
                            "importance": 0.5,
                            "provenance": 0,
                        }
                    },
                }
            ],
        }
    ]
    result = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed["memories"][0]["content"] == content_str
    assert parsed["memories"][0]["content_truncated"] is True
    assert parsed["memories"][0]["content_length"] == 1500
    assert content_str.endswith("-decisive-suffix")

    assert "content_truncated" not in parsed["memories"][1]
    assert "content_length" not in parsed["memories"][1]

    _assert_no_token_leak(result, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_recall_zero_results_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {"mode": "deep", "limit": 5, "warnings": [], "results": []}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_recall_zero_results_prose(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_nullable_metadata_preserved_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": [],
            "results": [
                {
                    "object_id": "mem_1",
                    "score": 0.9,
                    "content": "hello",
                    "namespace": f"{identity}/hermes/episodic",
                    "plane": "episodic",
                    "state": None,
                    "importance": None,
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
            ],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])
    _assert_nullable_metadata_preserved_json(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_read_401_auth(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 401
    mock_server.mock.responses = [
        {"error": {"code": "UNAUTHORIZED", "detail": "bad token"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_read_401_auth(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_write_403_scope(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 403
    mock_server.mock.responses = [
        {"error": {"code": "FORBIDDEN", "detail": "bad scope"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])
    _assert_write_403_scope(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_write_409_conflict(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 409
    mock_server.mock.responses = [
        {"error": {"code": "CONFLICT", "detail": "idempotency collision"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])
    _assert_write_409_conflict(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_read_422_validation(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 422
    mock_server.mock.responses = [
        {"error": {"code": "UNPROCESSABLE_ENTITY", "detail": "bad schema"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_read_422_validation(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_write_5xx_unavailable(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.status_code = 503
    mock_server.mock.responses = [
        {"error": {"code": "UNAVAILABLE", "detail": "server down"}}
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])
    _assert_write_5xx_unavailable(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_write_timeout(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.timeout_delay = 1.0  # 1 second HTTP timeout
    res = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "test"],
        env={"MUSUBI_TIMEOUT": "0.1"},
    )  # CLI expects 0.1s
    _assert_write_timeout(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_read_malformed_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.malformed_json = True
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_read_malformed_json(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_write_local_enqueue_crash(mock_server, tmp_path, identity: str) -> None:
    db_path = tmp_path / "outbox.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("corrupted sqlite file")
    res = _run_cli(mock_server, tmp_path, identity, ["remember", "test"])
    _assert_write_local_enqueue_crash(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_warning_allowlist_dedup_prose(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": [
                "sparse_embedding_failed",
                "sparse_embedding_failed",
                "reranker_failed",
            ],
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_warning_allowlist_dedup_prose(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_warning_allowlist_dedup_json(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": ["sparse_embedding_failed", "sparse_embedding_failed"],
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])
    _assert_warning_allowlist_dedup_json(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_warning_bounds_cap_fails_closed(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": [f"plane_timeout_{i}" for i in range(25)],  # Max is 20 raw
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["recall", "test"])
    _assert_warning_bounds_cap_fails_closed(res, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_unknown_warning_fails_closed(mock_server, tmp_path, identity: str) -> None:
    mock_server.mock.responses = [
        {
            "mode": "deep",
            "limit": 5,
            "warnings": ["unrecognized_hax_code"],
            "results": [],
        }
    ]
    res = _run_cli(mock_server, tmp_path, identity, ["--json", "recall", "test"])
    _assert_unknown_warning_fails_closed(res, mock_server, tmp_path, identity)


# --- SQLite Crash Recovery Tests (Runs Dummy Shell Subprocesses that die at specific states) ---


def _run_crash_script_test(
    mock_server,
    tmp_path,
    identity,
    script_body,
    expected_post_count,
    expected_get_count,
):
    script = tmp_path / "crash_sim.py"
    script.write_text(script_body)

    # 1. Run the script that crashes
    res1 = _run_cli(
        mock_server, tmp_path, identity, ["remember", "x"], executable=str(script)
    )
    assert res1.returncode != 0

    # 2. Re-run to verify recovery
    mock_server.mock.responses = [{"object_id": "obj_123"}]  # Standard positive return
    script2 = tmp_path / "recover.py"
    # A correct script completing the cycle
    script2.write_text("""import sys, os, urllib.request, json
token = os.environ["MUSUBI_TOKEN"]
req = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic", data=json.dumps({"namespace": sys.argv[2] + "/hermes/episodic", "content": "x"}).encode(), headers={"Idempotency-Key": "123", "Authorization": f"Bearer {token}"}, method="POST")
try:
    urllib.request.urlopen(req)
except Exception:
    pass
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
print("remembered ✓  (id obj_123)")
sys.exit(0)
""")
    res2 = _run_cli(
        mock_server, tmp_path, identity, ["remember", "x"], executable=str(script2)
    )
    assert res2.returncode == 0

    # Check total network calls across both runs
    assert (
        sum(1 for r in mock_server.mock.requests if r["method"] == "POST")
        == expected_post_count
    )
    assert (
        sum(1 for r in mock_server.mock.requests if r["method"] == "GET")
        == expected_get_count
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_enqueue_before_claim(
    mock_server, tmp_path, identity: str
) -> None:
    # Simulates crash after row insertion (state=pending)
    script_body = """import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('pending')")
con.commit()
os._exit(1)
"""
    _run_crash_script_test(mock_server, tmp_path, identity, script_body, 1, 1)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_claim_before_post(
    mock_server, tmp_path, identity: str
) -> None:
    # Simulates crash after lease claim (state=inflight)
    script_body = """import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('inflight')")
con.commit()
os._exit(1)
"""
    _run_crash_script_test(mock_server, tmp_path, identity, script_body, 1, 1)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_post_before_accept(
    mock_server, tmp_path, identity: str
) -> None:
    # Simulates crash after HTTP POST but before local mark_accepted (state=inflight, 1 POST already issued)
    script_body = """import sys, sqlite3, os, urllib.request, json
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)")
con.execute("INSERT INTO outbox (state) VALUES ('inflight')")
con.commit()

token = os.environ["MUSUBI_TOKEN"]
req = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic", data=json.dumps({"namespace": sys.argv[2] + "/hermes/episodic", "content": "x"}).encode(), headers={"Idempotency-Key": "123", "Authorization": f"Bearer {token}"}, method="POST")
urllib.request.urlopen(req)
os._exit(1)
"""
    # 1 POST from script1, 1 POST from script2 (UUID match returns 200), 1 GET from script2 = (2 POST, 1 GET)
    _run_crash_script_test(mock_server, tmp_path, identity, script_body, 2, 1)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_accept_before_get(
    mock_server, tmp_path, identity: str
) -> None:
    # Simulates crash after mark_accepted but before GET
    script_body = """import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT, object_id TEXT)")
con.execute("INSERT INTO outbox (state, object_id) VALUES ('accepted', 'obj_123')")
con.commit()
os._exit(1)
"""
    # 0 POST from script1, 0 POST from script2 (skips), 1 GET from script2 = (0 POST, 1 GET)

    script2 = tmp_path / "recover.py"
    script2.write_text("""import sys, os, urllib.request, json
token = os.environ["MUSUBI_TOKEN"]
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
print("remembered ✓  (id obj_123)")
sys.exit(0)
""")
    res1 = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "x"],
        executable=tmp_path / "crash_sim.py"
        if not (tmp_path / "crash_sim.py").write_text(script_body)
        else tmp_path / "crash_sim.py",
    )
    assert res1.returncode != 0

    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res2 = _run_cli(
        mock_server, tmp_path, identity, ["remember", "x"], executable=str(script2)
    )
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 1


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_get_before_verify(
    mock_server, tmp_path, identity: str
) -> None:
    # Simulates crash after GET but before mark_verify
    script_body = """import sys, sqlite3, os, urllib.request
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT, object_id TEXT)")
con.execute("INSERT INTO outbox (state, object_id) VALUES ('accepted', 'obj_123')")
con.commit()
token = os.environ["MUSUBI_TOKEN"]
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
os._exit(1)
"""
    # 0 POST from script1, 1 GET from script1
    # 0 POST from script2, 1 GET from script2
    # Total: 0 POST, 2 GET
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res1 = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "x"],
        executable=tmp_path / "crash_sim.py"
        if not (tmp_path / "crash_sim.py").write_text(script_body)
        else tmp_path / "crash_sim.py",
    )
    assert res1.returncode != 0

    script2 = tmp_path / "recover.py"
    script2.write_text("""import sys, os, urllib.request, json
token = os.environ["MUSUBI_TOKEN"]
req2 = urllib.request.Request(os.environ["MUSUBI_API_URL"] + "/episodic/obj_123", headers={"Authorization": f"Bearer {token}"}, method="GET")
urllib.request.urlopen(req2)
print("remembered ✓  (id obj_123)")
sys.exit(0)
""")
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    res2 = _run_cli(
        mock_server, tmp_path, identity, ["remember", "x"], executable=str(script2)
    )
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 2


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_harness_crash_after_verify(mock_server, tmp_path, identity: str) -> None:
    # State verified -> zero execution
    script_body = """import sys, sqlite3, os
db_path = os.environ["ADAPT_DB_PATH"]
con = sqlite3.connect(db_path)
con.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT, object_id TEXT)")
con.execute("INSERT INTO outbox (state, object_id) VALUES ('verified', 'obj_123')")
con.commit()
sys.exit(0)
"""
    script = tmp_path / "setup.py"
    script.write_text(script_body)
    _run_cli(mock_server, tmp_path, identity, ["remember", "x"], executable=str(script))

    # Recover should do 0 work
    script2 = tmp_path / "recover.py"
    script2.write_text("""import sys
sys.exit(0)
""")
    res2 = _run_cli(
        mock_server, tmp_path, identity, ["remember", "x"], executable=str(script2)
    )
    assert res2.returncode == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "POST") == 0
    assert sum(1 for r in mock_server.mock.requests if r["method"] == "GET") == 0


def _assert_production_crash_recovery(
    mock_server,
    tmp_path: Path,
    identity: str,
    checkpoint: str,
    expected_posts: int,
    expected_episodic_posts: int,
    expected_gets: int,
) -> None:
    operation_id = f"crash-{identity}-{checkpoint}"
    post = {"object_id": "obj_123"}
    get = {
        "object_id": "obj_123",
        "namespace": f"{identity}/hermes/episodic",
        "content": "test",
    }
    response_shapes = {
        "after_enqueue_before_claim": [post, get],
        "after_claim_before_post": [
            {"mode": "recent", "limit": 50, "warnings": [], "results": []},
            post,
            get,
        ],
        "after_post_before_accept": [
            post,
            {
                "mode": "recent",
                "limit": 50,
                "warnings": [],
                "results": [{"object_id": "obj_123"}],
            },
            get,
        ],
        "after_accept_before_get": [post, get],
        "after_get_before_verify": [post, get, get],
        "after_verify": [post, get],
    }
    mock_server.mock.responses = list(response_shapes[checkpoint])

    crashed = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "test"],
        env={"ADAPT_CRASH_AT": checkpoint},
        operation_id=operation_id,
    )
    assert crashed.returncode == 91

    recovered = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "test"],
        operation_id=operation_id,
    )
    assert recovered.returncode == 0
    assert recovered.stdout.strip() == "remembered ✓  (id obj_123)"
    assert (
        sum(r["method"] == "POST" for r in mock_server.mock.requests) == expected_posts
    )
    assert sum(r["method"] == "GET" for r in mock_server.mock.requests) == expected_gets
    episodic_posts = [
        r
        for r in mock_server.mock.requests
        if r["method"] == "POST" and r["path"] == "/v1/episodic"
    ]
    retrieve_posts = [
        r
        for r in mock_server.mock.requests
        if r["method"] == "POST" and r["path"] == "/v1/retrieve"
    ]
    assert len(episodic_posts) == expected_episodic_posts
    assert len(retrieve_posts) == expected_posts - expected_episodic_posts
    post_keys = [r["headers"]["Idempotency-Key"] for r in episodic_posts]
    assert len(post_keys) == expected_episodic_posts
    assert len(set(post_keys)) == 1
    with sqlite3.connect(tmp_path / "outbox.db") as con:
        assert con.execute("SELECT state FROM outbox").fetchone()[0] == "verified"
    _assert_no_token_leak(recovered, mock_server, tmp_path, identity)


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_enqueue_before_claim(
    mock_server, tmp_path, identity: str
) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_enqueue_before_claim", 1, 1, 1
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_claim_before_post(
    mock_server, tmp_path, identity: str
) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_claim_before_post", 2, 1, 1
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_post_before_accept(
    mock_server, tmp_path, identity: str
) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_post_before_accept", 2, 1, 1
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_accept_before_get(
    mock_server, tmp_path, identity: str
) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_accept_before_get", 1, 1, 1
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_get_before_verify(
    mock_server, tmp_path, identity: str
) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_get_before_verify", 1, 1, 2
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_crash_target_after_verify(mock_server, tmp_path, identity: str) -> None:
    _assert_production_crash_recovery(
        mock_server, tmp_path, identity, "after_verify", 1, 1, 1
    )


@pytest.mark.parametrize("identity", ["nyla", "sumi"])
def test_recovery_refuses_degraded_receipt_lookup_before_repost(
    mock_server, tmp_path, identity: str
) -> None:
    operation_id = f"degraded-receipt-{identity}"
    mock_server.mock.responses = [{"object_id": "obj_123"}]
    crashed = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "test"],
        env={"ADAPT_CRASH_AT": "after_post_before_accept"},
        operation_id=operation_id,
    )
    assert crashed.returncode == 91

    mock_server.mock.responses = [
        {
            "mode": "recent",
            "limit": 50,
            "warnings": ["reranker_failed"],
            "results": [],
        }
    ]
    recovered = _run_cli(
        mock_server,
        tmp_path,
        identity,
        ["remember", "test"],
        operation_id=operation_id,
    )
    assert recovered.returncode == 2
    assert sum(r["path"] == "/v1/episodic" for r in mock_server.mock.requests) == 1
    assert sum(r["path"] == "/v1/retrieve" for r in mock_server.mock.requests) == 1
    with sqlite3.connect(tmp_path / "outbox.db") as con:
        row = con.execute("SELECT state, attempts FROM outbox").fetchone()
    # One durable orphan-recovery attempt plus one deferred receipt-check failure.
    assert row == ("pending", 2)
    _assert_no_token_leak(recovered, mock_server, tmp_path, identity)
