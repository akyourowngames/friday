"""Tests for SQLite connection helpers."""

import sqlite3

from ares.infra.sqlite_utils import connect_sqlite, retry_sqlite_locked


def test_connect_sqlite_sets_busy_timeout_and_row_factory(tmp_path):
    conn = connect_sqlite(tmp_path / "ares.db")
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        row = conn.execute("SELECT 1 AS value").fetchone()
        assert timeout == 30000
        assert row["value"] == 1
    finally:
        conn.close()


def test_multiple_connections_can_write_same_database(tmp_path):
    path = tmp_path / "ares.db"
    first = connect_sqlite(path)
    second = connect_sqlite(path)
    try:
        first.execute("CREATE TABLE example (value TEXT)")
        first.commit()
        first.execute("INSERT INTO example (value) VALUES ('one')")
        first.commit()
        second.execute("INSERT INTO example (value) VALUES ('two')")
        second.commit()
        count = first.execute("SELECT COUNT(*) AS count FROM example").fetchone()["count"]
        assert count == 2
    finally:
        first.close()
        second.close()


def test_retry_sqlite_locked_retries_transient_writer_collision():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ready"

    assert retry_sqlite_locked(operation, attempts=3, initial_delay=0) == "ready"
    assert attempts == 3


def test_retry_sqlite_locked_does_not_hide_unrelated_operational_error():
    def operation():
        raise sqlite3.OperationalError("no such table: goals_meta")

    try:
        retry_sqlite_locked(operation, attempts=3, initial_delay=0)
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    else:  # pragma: no cover - makes failure output clearer than pytest.raises here.
        raise AssertionError("unrelated SQLite error should not be retried")
