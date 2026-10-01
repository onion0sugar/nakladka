"""Mały lokalny stan, aby nie ponawiać tego samego przypisania kuriera."""

from __future__ import annotations

import sqlite3


def open_state(path: str = "state.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS courier_seen (doc_id INTEGER PRIMARY KEY, courier_id TEXT, status TEXT, user_name TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ready_acknowledged "
        "(doc_id INTEGER PRIMARY KEY, user_topic TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def load_ready_acknowledgements(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(doc_id): str(user_topic)
        for doc_id, user_topic in conn.execute(
            "SELECT doc_id, user_topic FROM ready_acknowledged"
        )
    }


def acknowledge_ready_order(conn: sqlite3.Connection, doc_id: int, user_topic: str) -> None:
    conn.execute(
        "INSERT INTO ready_acknowledged(doc_id, user_topic) VALUES(?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET user_topic=excluded.user_topic",
        (doc_id, user_topic),
    )
    conn.commit()


def clear_ready_acknowledgement(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM ready_acknowledged WHERE doc_id=?", (doc_id,))
    conn.commit()


def courier_changed(conn, doc_id: int, courier_id: str, status: str, user_name: str) -> bool:
    row = conn.execute("SELECT courier_id, status, user_name FROM courier_seen WHERE doc_id=?", (doc_id,)).fetchone()
    changed = row is None or tuple(row) != (courier_id, status, user_name)
    conn.execute(
        "INSERT INTO courier_seen(doc_id,courier_id,status,user_name) VALUES(?,?,?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET courier_id=excluded.courier_id,status=excluded.status,user_name=excluded.user_name,updated_at=CURRENT_TIMESTAMP",
        (doc_id, courier_id, status, user_name),
    )
    conn.commit()
    return changed
