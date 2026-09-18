"""Gate tests: happy path, every fault injection, and recovery.

These tests intentionally use the standard library only (temp sqlite DBs +
a local HTTP stub), so they run unchanged under ``pytest`` and can also be
executed directly with ``python -m app.tests.test_gate`` in environments
where pytest is unavailable (e.g. a bare CI step).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import db, gate, seed

BACKEND_DIR = Path(__file__).resolve().parents[2]

HEALTH_BODY = {"ok": True, "project": "ladderbill"}


class _HealthHandler(BaseHTTPRequestHandler):
    response_payload = HEALTH_BODY
    response_status = 200

    def do_GET(self):
        if self.path != gate.HEALTH_PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HealthStub:
    """Tiny /api/health stand-in; close() simulates the service going down."""

    def __init__(self, payload=HEALTH_BODY, status=200):
        handler = type(
            "ConfiguredHandler",
            (_HealthHandler,),
            {"response_payload": payload, "response_status": status},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextlib.contextmanager
def seeded_db():
    """Point the app at a freshly seeded temp DB for the duration of the test."""
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "app.db"
    original = db.DB_PATH
    db.DB_PATH = db_path
    try:
        seed.init_db()
        yield db_path
    finally:
        db.DB_PATH = original
        tmp.cleanup()


def _raw_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _run_gate(base_url: str) -> list[str]:
    return gate.run_checks(base_url=base_url)


# ---------------------------------------------------------------- happy path


def test_seeded_db_and_healthy_service_pass():
    with seeded_db():
        stub = HealthStub()
        try:
            assert _run_gate(stub.base_url) == []
        finally:
            stub.close()


def test_repeatable_two_consecutive_runs():
    with seeded_db():
        stub = HealthStub()
        try:
            first = _run_gate(stub.base_url)
            second = _run_gate(stub.base_url)
            assert first == [] and second == []
        finally:
            stub.close()


# ------------------------------------------------------- account fault cases


def test_missing_dirty_account_fails():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            with _raw_conn(db_path) as conn:
                # the dirty household is the one whose seed name carries 种子
                row = conn.execute(
                    "SELECT id FROM accounts WHERE name LIKE '%种子%'"
                ).fetchone()
                assert row is not None, "fixture must seed a dirty household"
                conn.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
                conn.commit()

            missing = _run_gate(stub.base_url)
            assert gate.MISSING_ACCOUNT_DIRTY in missing
            assert gate.MISSING_ACCOUNT_CLEAN not in missing
        finally:
            stub.close()


def test_missing_clean_account_fails():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            with _raw_conn(db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE name NOT LIKE '%种子%'"
                ).fetchone()
                assert row is not None, "fixture must seed a clean household"
                conn.execute("DELETE FROM accounts WHERE id=?", (row["id"],))
                conn.commit()

            missing = _run_gate(stub.base_url)
            assert gate.MISSING_ACCOUNT_CLEAN in missing
            assert gate.MISSING_ACCOUNT_DIRTY not in missing
        finally:
            stub.close()


def test_restore_dirty_account_returns_to_green():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            with _raw_conn(db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE name LIKE '%种子%'"
                ).fetchone()
                saved = dict(row)
                conn.execute("DELETE FROM accounts WHERE id=?", (saved["id"],))
                conn.commit()

            assert gate.MISSING_ACCOUNT_DIRTY in _run_gate(stub.base_url)

            # restore the seed row verbatim (values untouched) -> green again
            with _raw_conn(db_path) as conn:
                conn.execute(
                    "INSERT INTO accounts(id, name, meter_no, note) VALUES (?,?,?,?)",
                    (saved["id"], saved["name"], saved["meter_no"], saved["note"]),
                )
                conn.commit()

            assert _run_gate(stub.base_url) == []
        finally:
            stub.close()


def test_reseed_after_full_wipe_restores_green():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            with _raw_conn(db_path) as conn:
                conn.execute("DELETE FROM accounts")
                conn.commit()

            missing = _run_gate(stub.base_url)
            assert gate.MISSING_ACCOUNT_DIRTY in missing
            assert gate.MISSING_ACCOUNT_CLEAN in missing

            # operator recovery: empty every seeded table and re-run seeder
            with _raw_conn(db_path) as conn:
                conn.execute("DELETE FROM accounts")
                conn.execute("DELETE FROM tiers")
                conn.execute("DELETE FROM readings")
                conn.execute("DELETE FROM settings")
                conn.execute("DELETE FROM calc_runs")
                conn.commit()
            seed.init_db()

            assert _run_gate(stub.base_url) == []
        finally:
            stub.close()


# ---------------------------------------------------------- tier fault cases


def test_missing_tier_band_fails():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            with _raw_conn(db_path) as conn:
                conn.execute("DELETE FROM tiers WHERE sort_order=2")
                conn.commit()

            missing = _run_gate(stub.base_url)
            assert gate.MISSING_TIERS_COUNT in missing
        finally:
            stub.close()


def test_non_monotonic_up_to_fails():
    with seeded_db() as db_path:
        stub = HealthStub()
        try:
            # keep three rows and the seeded prices, only break the bound
            with _raw_conn(db_path) as conn:
                conn.execute("UPDATE tiers SET up_to=120 WHERE sort_order=2")
                conn.commit()

            missing = _run_gate(stub.base_url)
            assert gate.MISSING_TIERS_MONOTONIC in missing
            assert gate.MISSING_TIERS_COUNT not in missing
        finally:
            stub.close()


# --------------------------------------------------------- health fault cases


def test_service_down_fails():
    with seeded_db():
        stub = HealthStub()
        dead_url = stub.base_url
        stub.close()  # inject: service stopped

        missing = _run_gate(dead_url)
        assert missing == [gate.MISSING_HEALTH_REACHABLE]


def test_health_restored_after_service_restart():
    with seeded_db():
        stub = HealthStub()
        url = stub.base_url
        stub.close()
        assert gate.MISSING_HEALTH_REACHABLE in _run_gate(url)

        # service comes back -> green again (a healthy target is what matters)
        restarted = HealthStub()
        try:
            assert _run_gate(restarted.base_url) == []
        finally:
            restarted.close()


def test_wrong_project_flag_fails():
    with seeded_db():
        stub = HealthStub(payload={"ok": True, "project": "something-else"})
        try:
            assert _run_gate(stub.base_url) == [gate.MISSING_HEALTH_PROJECT]
        finally:
            stub.close()


def test_health_ok_false_flag_fails():
    with seeded_db():
        stub = HealthStub(payload={"ok": False, "project": "ladderbill"})
        try:
            assert _run_gate(stub.base_url) == [gate.MISSING_HEALTH_OK]
        finally:
            stub.close()


# --------------------------------------------------------------- CLI / exit


def _run_cli(data_dir: Path, base_url: str):
    env = dict(os.environ)
    env["DATA_DIR"] = str(data_dir)
    env["BASE_URL"] = base_url
    env["PYTHONPATH"] = str(BACKEND_DIR)
    return subprocess.run(
        [sys.executable, "-m", "app.gate"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _seed_in_dir(data_dir: Path) -> Path:
    db_path = data_dir / "app.db"
    original = db.DB_PATH
    db.DB_PATH = db_path
    try:
        seed.init_db()
    finally:
        db.DB_PATH = original
    return db_path


def test_cli_green_twice_exits_zero_with_empty_missing():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        _seed_in_dir(data_dir)

        stub = HealthStub()
        try:
            first = _run_cli(data_dir, stub.base_url)
            second = _run_cli(data_dir, stub.base_url)
        finally:
            stub.close()

        for result in (first, second):
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout.splitlines()[0])
            assert report["missing"] == []
            assert result.stderr == ""


def test_cli_red_names_item_on_stderr_and_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        _seed_in_dir(data_dir)

        stub = HealthStub()
        dead_url = stub.base_url
        stub.close()  # inject: service stopped

        result = _run_cli(data_dir, dead_url)
        assert result.returncode != 0
        # the missing item itself must appear on stderr, not a generic failure
        assert gate.MISSING_HEALTH_REACHABLE in result.stderr
        assert json.loads(result.stdout)["ok"] is False
        assert gate.MISSING_HEALTH_REACHABLE in json.loads(result.stdout)["missing"]


# ---------------------------------------- stdlib shim: run without pytest ---


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
