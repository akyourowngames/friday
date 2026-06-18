"""Tests for SQLite connection helpers."""

from ares.sqlite_utils import connect_sqlite


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
