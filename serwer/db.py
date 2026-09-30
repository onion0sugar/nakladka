"""Warstwa MSSQL: połączenie (pyodbc) + zapytanie z osobnego pliku query.sql.

Baza jest traktowana jako TYLKO DO ODCZYTU — wykonujemy wyłącznie SELECT.
Connection string zawiera ApplicationIntent=ReadOnly (przy Always On
Availability Groups połączenie trafia do repliki do odczytu).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace

try:
    import pyodbc
except ImportError:
    pyodbc = None  # type: ignore[assignment]

logger = logging.getLogger("db")

QUERY_FILE = "query.sql"
COURIER_QUERY_FILE = "query22.sql"
READY_USERS_QUERY_FILE = "query22_users.sql"
WORK_TODAY_USERS_QUERY_FILE = "work_today_users.sql"


class DbError(Exception):
    """Błąd połączenia, zapytania lub pliku query.sql."""


# --- zapytanie w osobnym pliku ------------------------------------------------


def load_query(path: str = QUERY_FILE) -> str:
    """Wczytaj zapytanie z pliku. Fail-fast: brak pliku / pusty / nie-SELECT.

    Komentarze (-- ...) na początku pliku są pomijane przy walidacji i zwrocie.
    """
    if not os.path.isfile(path):
        raise DbError(
            f"Brak pliku zapytania: {path}. Utwórz go wg wzoru z repozytorium."
        )
    with open(path, encoding="utf-8") as f:
        sql = f.read().strip()
    if not sql:
        raise DbError(f"Plik zapytania {path} jest pusty.")
    # odrzuć wiodące linie komentarzy, żeby znaleźć właściwą instrukcję
    lines = sql.splitlines()
    while lines and lines[0].lstrip().startswith("--"):
        lines.pop(0)
    statement = "\n".join(lines).strip()
    if not statement:
        raise DbError(f"Plik zapytania {path} zawiera tylko komentarze.")
    # SQL Server pozwala poprzedzić SELECT definicją CTE, zwykle zapisaną jako
    # ";WITH ... SELECT ...". Taki wstęp jest nadal zapytaniem tylko do
    # odczytu, więc nie można wymagać, aby pierwszy token był SELECT.
    normalized = statement.lstrip()
    if normalized.startswith(";"):
        normalized = normalized[1:].lstrip()
    first_token = normalized.split(None, 1)[0].upper() if normalized else ""
    if first_token not in {"SELECT", "WITH"}:
        raise DbError(f"Plik zapytania {path} musi zaczynać się od SELECT lub WITH.")
    return statement


# --- połączenie ----------------------------------------------------------------


def build_dsn(cfg: SimpleNamespace) -> str:
    """Connection string ODBC Driver 18.

    Instancja nazwana (host\\instancja) NIE dostaje portu w adresie — numer
    portu odczytuje SQL Browser (UDP 1434). Port dopisujemy tylko zwykłemu
    adresowi.
    """
    server = cfg.mssql_server
    if "\\" not in server:
        server = f"{server},{cfg.mssql_port}"
    return ";".join(
        [
            "DRIVER={ODBC Driver 18 for SQL Server}",
            f"SERVER={server}",
            f"DATABASE={cfg.mssql_database}",
            f"UID={cfg.mssql_username}",
            f"PWD={cfg.mssql_password}",
            f"Encrypt={cfg.mssql_encrypt}",
            f"TrustServerCertificate={cfg.mssql_trust_server_certificate}",
            "ApplicationIntent=ReadOnly",
        ]
    )


def connect_db(cfg: SimpleNamespace):
    """Połączenie pyodbc (autocommit — każda instrukcja to własna transakcja)."""
    if pyodbc is None:
        raise DbError("pyodbc nie jest zainstalowane — pip install -r requirements.txt")
    try:
        cnxn = pyodbc.connect(build_dsn(cfg), timeout=10, autocommit=True)
    except Exception as exc:
        raise DbError(f"MSSQL connection failed: {exc}") from exc
    cnxn.timeout = 15
    logger.info("Connected to MSSQL (%s)", cfg.mssql_server)
    return cnxn


# --- zapytanie ------------------------------------------------------------------


def get_next_order(cursor, query: str) -> tuple[int | None, str] | None:
    """Wykonaj zapytanie z query.sql. Wiersz → powiadomienie (bez pamiętania).

    Obsługuje zapytania zwracające:
    * 1 kolumnę — sam numer zamówienia (np. OriginalNumber),
    * 2 kolumny — id (liczba, tylko do logów) + numer zamówienia.

    Zwraca (order_id, order_number); order_id = None, gdy brak kolumny id
    lub nie jest liczbą.
    """
    try:
        cursor.execute(query)
        row = cursor.fetchone()
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc
    if row is None:
        return None
    if len(row) >= 2:
        order_number = "" if row[1] is None else str(row[1])
        try:
            order_id: int | None = int(row[0])
        except (TypeError, ValueError):
            order_id = None  # nie-liczbowe id — niepotrzebne do wysyłki
        return order_id, order_number
    # jedna kolumna — sam numer zamówienia
    order_number = "" if row[0] is None else str(row[0])
    return None, order_number


def get_new_orders(cursor, query: str) -> list[tuple[int | None, str]]:
    """Wykonaj query.sql i zwróć wszystkie znalezione zamówienia."""
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc
    result = []
    for row in rows:
        if len(row) >= 2:
            order_number = "" if row[1] is None else str(row[1])
            try:
                order_id: int | None = int(row[0])
            except (TypeError, ValueError):
                order_id = None
            result.append((order_id, order_number))
        elif row:
            result.append((None, "" if row[0] is None else str(row[0])))
    return result


@dataclass(frozen=True)
class CourierRow:
    doc_id: int | None
    number: str
    document_type: str
    courier_id: str
    status: str
    user_name: str
    item_count: int
    zone_group_id: int | None


def fetch_column_rows(cursor, query: str, required: tuple[str, ...], params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    try:
        cursor.execute(query, params) if params else cursor.execute(query)
        description = cursor.description or []
        names = [str(col[0]).strip().lower() for col in description]
        indices = {name: names.index(name) for name in required if name in names}
        missing = [name for name in required if name not in indices]
        if missing:
            raise DbError(f"Zapytanie musi zwracać kolumny: {', '.join(missing)}")
        return [{name: row[idx] for name, idx in indices.items()} for row in cursor.fetchall()]
    except DbError:
        raise
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc


def fetch_busy_users(cursor, query: str) -> set[str]:
    return {str(row["username"]).strip() for row in fetch_column_rows(cursor, query, ("username",)) if row["username"]}


def fetch_work_today_users(cursor, query: str, users: set[str]) -> dict[str, int]:
    """Return active users and their highest assigned zone group."""
    if not users:
        return {}
    placeholders = ", ".join("?" for _ in users)
    try:
        cursor.execute(query.replace("{usernames}", placeholders), tuple(sorted(users)))
        rows = cursor.fetchall()
    except Exception as exc:
        raise DbError(f"Query failed: {exc}") from exc
    result: dict[str, int] = {}
    for row in rows:
        if not row or not row[0] or row[1] is None:
            continue
        try:
            result[str(row[0]).strip()] = int(row[1])
        except (TypeError, ValueError):
            continue
    return result


def fetch_top_ready_user(cursor, query: str, original_number: str) -> list[tuple[str, int, int]]:
    rows = fetch_column_rows(cursor, query, ("username", "packagedpositioncount", "id"), (original_number,))
    result: list[tuple[str, int, int]] = []
    for row in rows:
        if not row["username"]:
            continue
        try:
            count = int(row["packagedpositioncount"] or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            document_id = int(row["id"])
        except (TypeError, ValueError):
            continue
        result.append((str(row["username"]).strip(), count, document_id))
    return result


def fetch_courier_rows(cursor, query: str) -> list[CourierRow]:
    rows = fetch_column_rows(cursor, query, ("id", "originalnumber", "documenttype", "courierid", "documentstatustext", "username", "ilepozycji", "zonegroupid"))
    result = []
    for row in rows:
        try:
            doc_id = int(row["id"]) if row["id"] is not None else None
        except (TypeError, ValueError):
            doc_id = None
        try:
            item_count = int(row["ilepozycji"] or 0)
        except (TypeError, ValueError):
            item_count = 0
        try:
            zone_group_id = int(row["zonegroupid"]) if row["zonegroupid"] is not None else None
        except (TypeError, ValueError):
            zone_group_id = None
        result.append(CourierRow(doc_id, str(row["originalnumber"] or "").strip(), str(row["documenttype"] or "").strip(), str(row["courierid"] or "").strip(), str(row["documentstatustext"] or "").strip().lower().replace(" ", "_"), str(row["username"] or "").strip(), item_count, zone_group_id))
    return result
