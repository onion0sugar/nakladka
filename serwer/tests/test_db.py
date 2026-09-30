"""Testy warstwy bazy: DSN (instancja nazwana), query.sql, zapytania, połączenie.

pyodbc jest zamockowane — bez prawdziwego MSSQL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import db
from config import load_config
from db import DbError, build_dsn, get_next_order, load_query


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._result = self.row

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, cursor):
        self.cursor = cursor
        self.timeout = None
        self.closed = False

    def cursor(self):
        return self.cursor

    def close(self):
        self.closed = True


class FakePyodbc:
    def __init__(self, fail=False):
        self.fail = fail
        self.last_dsn = None

    def connect(self, dsn, timeout=None, autocommit=False):
        self.last_dsn = dsn
        if self.fail:
            raise Exception("08001 can't connect")  # symulacja pyodbc.Error
        return FakeConnection(FakeCursor(None))


def make_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        mssql_server="192.168.24.22\\SERWISKOPB2B",
        mssql_port=1433,
        mssql_database="SerwisKop_Magazyn",
        mssql_username="serwiskop-ro",
        mssql_password="haslo",
        mssql_encrypt="yes",
        mssql_trust_server_certificate="yes",
        zello_network="net",
        zello_username="bot",
        zello_password="pw",
        zello_channel="Magazyn",
        zello_auth_token="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- DSN -----------------------------------------------------------------------


def test_dsn_named_instance_without_port():
    dsn = build_dsn(make_cfg(mssql_server="192.168.24.22\\SERWISKOPB2B"))
    assert "SERVER=192.168.24.22\\SERWISKOPB2B;" in dsn
    assert ",1433" not in dsn  # instancja nazwana: port ustala SQL Browser
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in dsn
    assert "DATABASE=SerwisKop_Magazyn" in dsn
    assert "UID=serwiskop-ro" in dsn
    assert "ApplicationIntent=ReadOnly" in dsn


def test_dsn_plain_server_gets_port():
    dsn = build_dsn(make_cfg(mssql_server="10.0.0.1"))
    assert "SERVER=10.0.0.1,1433;" in dsn


def test_dsn_custom_port():
    dsn = build_dsn(make_cfg(mssql_server="10.0.0.1", mssql_port=52341))
    assert "SERVER=10.0.0.1,52341;" in dsn


# --- query.sql ------------------------------------------------------------------


def test_query_file_ships_valid_select():
    """Wzorcowy query.sql z repozytorium ładuje się i zwraca numer zamówienia."""
    sql = load_query()  # realny plik w projekcie
    assert sql.lstrip().upper().startswith("SELECT")
    assert "OriginalNumber" in sql


def test_load_query_missing_file_raises(tmp_path):
    with pytest.raises(DbError, match="Brak pliku zapytania"):
        load_query(str(tmp_path / "brak.sql"))


def test_load_query_empty_file_raises(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(DbError, match="pusty"):
        load_query(str(path))


def test_load_query_non_select_raises(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("DELETE FROM dbo.orders", encoding="utf-8")
    with pytest.raises(DbError, match="SELECT"):
        load_query(str(path))


def test_load_query_reads_file(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text("SELECT 1", encoding="utf-8")
    assert load_query(str(path)) == "SELECT 1"


def test_load_query_accepts_select_with_cte(tmp_path):
    path = tmp_path / "q.sql"
    path.write_text(";WITH rows AS (SELECT 1 AS value) SELECT value FROM rows", encoding="utf-8")
    assert load_query(str(path)).startswith(";WITH")


# --- zapytanie -------------------------------------------------------------------


def test_get_next_order_returns_row():
    cursor = FakeCursor((101, "ZAM/2026/1234"))
    order = get_next_order(cursor, "SELECT TOP 1 id, order_number FROM dbo.orders")
    assert order == (101, "ZAM/2026/1234")
    sql, params = cursor.executed[0]
    assert sql == "SELECT TOP 1 id, order_number FROM dbo.orders"
    assert params is None  # bez parametrów — bot nie pamięta last_id


def test_get_next_order_none_when_no_row():
    assert get_next_order(FakeCursor(None), "SELECT 1") is None


def test_get_next_order_null_order_number_becomes_empty():
    order = get_next_order(FakeCursor((101, None)), "SELECT 1")
    assert order == (101, "")


def test_get_next_order_single_column_only():
    """Zapytanie zwraca sam numer (np. OriginalNumber) — id = None."""
    order = get_next_order(FakeCursor(("ZAM/2026/1234",)), "SELECT 1")
    assert order == (None, "ZAM/2026/1234")


def test_get_next_order_non_numeric_id_is_tolerated():
    row = ("ZAM/2026/1234", "Klient")  # 2 kolumny, id nie jest liczbą
    order = get_next_order(FakeCursor(row), "SELECT 1")
    assert order == (None, "Klient")  # id tylko do logów — nie blokuje wysyłki


def test_get_next_order_wraps_db_errors():
    class BoomCursor:
        def execute(self, sql, params=None):
            raise RuntimeError("table not found")

    with pytest.raises(DbError, match="Query failed"):
        get_next_order(BoomCursor(), "SELECT 1")


# --- connect_db -------------------------------------------------------------------


def test_connect_db_uses_dsn_and_autocommit(monkeypatch):
    fake = FakePyodbc()
    monkeypatch.setattr(db, "pyodbc", fake)
    cnxn = db.connect_db(make_cfg())
    assert fake.last_dsn and "SERVER=192.168.24.22\\SERWISKOPB2B;" in fake.last_dsn
    assert cnxn.timeout == 15


def test_connect_db_failure_raises_db_error(monkeypatch):
    fake = FakePyodbc(fail=True)
    monkeypatch.setattr(db, "pyodbc", fake)
    with pytest.raises(DbError, match="connection failed"):
        db.connect_db(make_cfg())
