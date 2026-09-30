"""Persistent sequential order dispatcher for ntfy offers."""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from typing import Iterable

OFFER_VISIBLE_SECONDS = 15
SEND_CYCLE_SECONDS = 16


@dataclass(frozen=True)
class DispatchAction:
    kind: str
    order_id: int
    order_number: str
    zone_group_id: int
    user_topic: str | None = None
    round_number: int = 0
    accepted_by: str | None = None
    assignment_count: int = 0
    rejected_counts: tuple[tuple[str, int], ...] = ()
    expired_counts: tuple[tuple[str, int], ...] = ()


class OrderDispatcher:
    """Stores attempts and guarantees at most one active offer per user."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS order_dispatch ("
            "order_id INTEGER PRIMARY KEY, order_number TEXT NOT NULL, "
            "zone_group_id INTEGER NOT NULL, round_number INTEGER NOT NULL DEFAULT 1, "
            "round_users TEXT NOT NULL DEFAULT '[]', attempted_users TEXT NOT NULL DEFAULT '[]', "
            "status TEXT NOT NULL DEFAULT 'pending', candidate_user TEXT, retry_at REAL, "
            "active_user TEXT, offered_at REAL, next_offer_at REAL NOT NULL DEFAULT 0, "
            "assignment_count INTEGER NOT NULL DEFAULT 0, "
            "rejected_counts TEXT NOT NULL DEFAULT '{}', "
            "expired_counts TEXT NOT NULL DEFAULT '{}', accepted_by TEXT)"
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(order_dispatch)")
        }
        if "assignment_count" not in columns:
            self.connection.execute(
                "ALTER TABLE order_dispatch ADD COLUMN assignment_count INTEGER NOT NULL DEFAULT 0"
            )
        if "rejected_counts" not in columns:
            self.connection.execute(
                "ALTER TABLE order_dispatch ADD COLUMN rejected_counts TEXT NOT NULL DEFAULT '{}'"
            )
        if "expired_counts" not in columns:
            self.connection.execute(
                "ALTER TABLE order_dispatch ADD COLUMN expired_counts TEXT NOT NULL DEFAULT '{}'"
            )
        if "accepted_by" not in columns:
            self.connection.execute(
                "ALTER TABLE order_dispatch ADD COLUMN accepted_by TEXT"
            )
        self.connection.commit()

    @staticmethod
    def _decode(value: str | None) -> list[str]:
        decoded = json.loads(value or "[]")
        return [str(item) for item in decoded] if isinstance(decoded, list) else []

    @staticmethod
    def _decode_counts(value: str | None) -> dict[str, int]:
        decoded = json.loads(value or "{}")
        if not isinstance(decoded, dict):
            return {}
        return {
            str(user): int(count)
            for user, count in decoded.items()
            if isinstance(count, int) and count > 0
        }

    def busy_users(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT active_user, candidate_user FROM order_dispatch"
        ).fetchall()
        return {str(user) for row in rows for user in row if user}

    def reject(self, order_id: int, user_topic: str, now: float) -> bool:
        """Accept a rejection only from the user currently offered this order."""
        cursor = self.connection.execute(
            "UPDATE order_dispatch SET status='pending', active_user=NULL, "
            "candidate_user=NULL, retry_at=NULL, offered_at=NULL, next_offer_at=? "
            "WHERE order_id=? AND status='offered' AND active_user=?",
            (now, order_id, user_topic),
        )
        if cursor.rowcount == 1:
            row = self.connection.execute(
                "SELECT rejected_counts FROM order_dispatch WHERE order_id=?", (order_id,)
            ).fetchone()
            counts = self._decode_counts(row[0] if row else None)
            counts[user_topic] = counts.get(user_topic, 0) + 1
            self.connection.execute(
                "UPDATE order_dispatch SET rejected_counts=? WHERE order_id=?",
                (json.dumps(counts), order_id),
            )
        self.connection.commit()
        return cursor.rowcount == 1

    def accept(self, order_id: int, user_topic: str) -> DispatchAction | None:
        """Confirm an offer only from its current recipient and stop dispatching it."""
        row = self.connection.execute(
            "SELECT order_number, zone_group_id, assignment_count, rejected_counts, expired_counts "
            "FROM order_dispatch WHERE order_id=? AND status='offered' AND active_user=?",
            (order_id, user_topic),
        ).fetchone()
        if row is None:
            return None

        cursor = self.connection.execute(
            "UPDATE order_dispatch SET status='accepted', accepted_by=? "
            "WHERE order_id=? AND status='offered' AND active_user=?",
            (user_topic, order_id, user_topic),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return DispatchAction(
            "accepted",
            order_id,
            row[0],
            row[1],
            user_topic,
            accepted_by=user_topic,
            assignment_count=int(row[2]),
            rejected_counts=tuple(sorted(self._decode_counts(row[3]).items())),
            expired_counts=tuple(sorted(self._decode_counts(row[4]).items())),
        )

    def finish_publish(self, action: DispatchAction, success: bool, now: float) -> None:
        if action.kind != "offer" or not action.user_topic:
            raise ValueError("finish_publish requires an offer action")
        row = self.connection.execute(
            "SELECT attempted_users FROM order_dispatch "
            "WHERE order_id=? AND status='retry' AND candidate_user=?",
            (action.order_id, action.user_topic),
        ).fetchone()
        if row is None:
            return

        if success:
            attempted = self._decode(row[0])
            if action.user_topic not in attempted:
                attempted.append(action.user_topic)
            self.connection.execute(
                "UPDATE order_dispatch SET status='offered', active_user=candidate_user, "
                "candidate_user=NULL, retry_at=NULL, offered_at=?, next_offer_at=?, "
                "attempted_users=?, assignment_count=assignment_count+1 WHERE order_id=?",
                (
                    now,
                    now + SEND_CYCLE_SECONDS,
                    json.dumps(attempted),
                    action.order_id,
                ),
            )
        else:
            self.connection.execute(
                "UPDATE order_dispatch SET retry_at=? WHERE order_id=?",
                (now + SEND_CYCLE_SECONDS, action.order_id),
            )
        self.connection.commit()

    def advance(
        self,
        orders: Iterable[tuple[int | None, str, int | None]],
        users: set[str],
        work_today: dict[str, int],
        database_busy: set[str],
        now: float,
        accepted_orders: dict[int, str] | None = None,
    ) -> list[DispatchAction]:
        """Synchronize new orders, expire offers, and allocate available users."""
        accepted_orders = accepted_orders or {}
        live_orders = {
            int(order_id): (str(number), int(group_id))
            for order_id, number, group_id in orders
            if order_id is not None and group_id is not None and number
        }
        actions: list[DispatchAction] = []
        stored_ids = {
            int(row[0])
            for row in self.connection.execute("SELECT order_id FROM order_dispatch")
        }

        for order_id in stored_ids - live_orders.keys():
            row = self.connection.execute(
                "SELECT order_number, zone_group_id, active_user, candidate_user, status, "
                "assignment_count, rejected_counts, expired_counts "
                "FROM order_dispatch WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if row:
                user_topic = row[2] or row[3]
                if row[4] == "accepted":
                    pass
                elif order_id in accepted_orders:
                    actions.append(
                        DispatchAction(
                            "accepted",
                            order_id,
                            row[0],
                            row[1],
                            user_topic,
                            accepted_by=accepted_orders[order_id],
                            assignment_count=int(row[5]),
                            rejected_counts=tuple(sorted(self._decode_counts(row[6]).items())),
                            expired_counts=tuple(sorted(self._decode_counts(row[7]).items())),
                        )
                    )
                elif user_topic:
                    actions.append(
                        DispatchAction("dismiss", order_id, row[0], row[1], user_topic)
                    )
            self.connection.execute("DELETE FROM order_dispatch WHERE order_id=?", (order_id,))

        for order_id, (number, group_id) in live_orders.items():
            self.connection.execute(
                "INSERT INTO order_dispatch(order_id, order_number, zone_group_id) "
                "VALUES(?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                "order_number=excluded.order_number, zone_group_id=excluded.zone_group_id",
                (order_id, number, group_id),
            )
        rows = self.connection.execute(
            "SELECT order_id, order_number, zone_group_id, active_user, offered_at, expired_counts "
            "FROM order_dispatch WHERE status='offered'"
        ).fetchall()
        for order_id, number, group_id, active_user, offered_at, expired_counts in rows:
            if offered_at is not None and now >= offered_at + OFFER_VISIBLE_SECONDS:
                actions.append(
                    DispatchAction("expired", order_id, number, group_id, active_user)
                )
                counts = self._decode_counts(expired_counts)
                if active_user:
                    counts[active_user] = counts.get(active_user, 0) + 1
                self.connection.execute(
                    "UPDATE order_dispatch SET status='pending', active_user=NULL, "
                    "offered_at=NULL, expired_counts=? WHERE order_id=?",
                    (json.dumps(counts), order_id),
                )

        self.connection.commit()
        allocation_rows = self.connection.execute(
            "SELECT order_id, active_user, candidate_user FROM order_dispatch"
        ).fetchall()
        allocations = {
            str(user): int(order_id)
            for order_id, active_user, candidate_user in allocation_rows
            for user in (active_user or candidate_user,)
            if user
        }

        for order_id in sorted(live_orders):
            number, group_id = live_orders[order_id]
            row = self.connection.execute(
                "SELECT round_number, round_users, attempted_users, status, "
                "candidate_user, retry_at, next_offer_at "
                "FROM order_dispatch WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if row is None:
                continue
            round_number, round_users_json, attempted_json, status, candidate, retry_at, next_offer_at = row
            if status in {"offered", "accepted"} or now < next_offer_at:
                continue

            candidates = sorted(
                login
                for login in users
                if login in work_today and work_today[login] >= group_id
            )
            if not candidates:
                continue

            if status == "retry" and candidate:
                allocation_owner = allocations.get(candidate)
                if (
                    candidate in candidates
                    and candidate not in database_busy
                    and allocation_owner in (None, order_id)
                ):
                    if retry_at is not None and now < retry_at:
                        continue
                    actions.append(
                        DispatchAction("offer", order_id, number, group_id, candidate, round_number)
                    )
                    continue

                self.connection.execute(
                    "UPDATE order_dispatch SET status='pending', candidate_user=NULL, "
                    "retry_at=NULL WHERE order_id=?",
                    (order_id,),
                )
                if allocation_owner == order_id:
                    allocations.pop(candidate, None)
                status = "pending"
                candidate = None

            round_users = [user for user in self._decode(round_users_json) if user in candidates]
            round_users.extend(user for user in candidates if user not in round_users)
            attempted = self._decode(attempted_json)

            if all(user in attempted for user in candidates):
                actions.append(
                    DispatchAction("round_complete", order_id, number, group_id, None, round_number)
                )
                round_number += 1
                attempted = []
                round_users = list(candidates)
                random.shuffle(round_users)

            available = next(
                (
                    user
                    for user in round_users
                    if user not in attempted
                    and user not in database_busy
                    and user not in allocations
                ),
                None,
            )
            if available is None:
                self.connection.execute(
                    "UPDATE order_dispatch SET round_number=?, round_users=?, attempted_users=? "
                    "WHERE order_id=?",
                    (round_number, json.dumps(round_users), json.dumps(attempted), order_id),
                )
                continue

            self.connection.execute(
                "UPDATE order_dispatch SET round_number=?, round_users=?, attempted_users=?, "
                "status='retry', candidate_user=?, retry_at=? WHERE order_id=?",
                (
                    round_number,
                    json.dumps(round_users),
                    json.dumps(attempted),
                    available,
                    now,
                    order_id,
                ),
            )
            allocations[available] = order_id
            actions.append(
                DispatchAction("offer", order_id, number, group_id, available, round_number)
            )

        self.connection.commit()
        return actions