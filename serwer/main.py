"""MSSQL → ntfy: powiadomienia o nowych i gotowych zamówieniach."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from config import ConfigError, load_config
from db import COURIER_QUERY_FILE, READY_USERS_QUERY_FILE, WORK_TODAY_USERS_QUERY_FILE, DbError, connect_db, fetch_courier_rows, fetch_top_ready_user, fetch_work_today_users, load_query
from dispatch import DispatchAction, OrderDispatcher, SEND_CYCLE_SECONDS
from ntfy import Ntfy, NtfyError
from state import courier_changed, open_state
from users import load_users

logger = logging.getLogger("bot")
RECONNECT_DELAY = 5
DEFAULT_NEW_TEXT = "{}"
ORDER_URL = "https://it.serwis-kop.pl/magazyn/pl/warehouse/collectingcustomerorders/view/{}"


def build_new_order_messages(
    orders: list[tuple[int | None, str, int | None]],
    users: set[str],
    work_today: dict[str, int],
    busy: set[str],
    supervisor_topic: str,
) -> list[tuple[str, str, str, str, str | None]]:
    """Zbuduj powiadomienia, wybierając najwyższą kwalifikującą się grupę."""
    messages: list[tuple[str, str, str, str, str | None]] = []
    notified_groups: set[int | None] = set()
    orders_to_notify: list[tuple[int | None, str, int]] = []
    supervisor_notifications: list[tuple[str, str | None]] = []
    for order_id, order_number, zone_group_id in orders:
        # `orders` są posortowane od najstarszego. Dla jednej grupy
        # powiadamiamy tylko o pierwszym (najstarszym) zamówieniu.
        if zone_group_id in notified_groups:
            continue
        notified_groups.add(zone_group_id)
        if zone_group_id is not None:
            orders_to_notify.append((order_id, order_number, zone_group_id))
        click_url = ORDER_URL.format(order_id) if order_id is not None else None
        displayed_number = f"{order_number} (grupa: {zone_group_id if zone_group_id is not None else 'brak'})"
        text = DEFAULT_NEW_TEXT.format(displayed_number)
        supervisor_notifications.append((text, click_url))
        if zone_group_id is None:
            continue

    if supervisor_notifications:
        supervisor_text = "\n".join(text for text, _click_url in supervisor_notifications)
        supervisor_click = supervisor_notifications[0][1] if len(supervisor_notifications) == 1 else None
        messages.append((supervisor_topic, supervisor_text, "Nowe zamówienia", "high", supervisor_click))

    for login in users:
        if login not in work_today or login in busy:
            continue
        eligible_order = max(
            (order for order in orders_to_notify if order[2] <= work_today[login]),
            key=lambda order: order[2],
            default=None,
        )
        if eligible_order is None:
            continue
        order_id, order_number, zone_group_id = eligible_order
        click_url = ORDER_URL.format(order_id) if order_id is not None else None
        displayed_number = f"{order_number} (grupa: {zone_group_id})"
        text = DEFAULT_NEW_TEXT.format(displayed_number)
        messages.append((login, text, "Nowe zamówienie", "high", click_url))
    return messages


def merge_supervisor_messages(
    messages: list[tuple[str, str, str, str, str | None]],
    supervisor_topic: str,
) -> list[tuple[str, str, str, str, str | None]]:
    """Połącz wszystkie komunikaty nadzorcy w jedno powiadomienie."""
    supervisor_messages = [message for message in messages if message[0] == supervisor_topic]
    if len(supervisor_messages) <= 1:
        return messages

    remaining_messages = [message for message in messages if message[0] != supervisor_topic]
    supervisor_text = "\n\n".join(message[1] for message in supervisor_messages)
    priority = "max" if any(message[3] == "max" for message in supervisor_messages) else "high"
    remaining_messages.append((supervisor_topic, supervisor_text, "Nowe zamówienia", priority, None))
    return remaining_messages


async def _sleep_until(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _send_batch(ntfy: Ntfy, messages: list[tuple[str, str, str, str, str | None]]) -> None:
    for item in messages:
        try:
            await ntfy.publish_to(*item)
        except Exception as exc:
            logger.error("Notification to ntfy topic %s failed: %s", item[0], exc)


def test_mode_work_today(users: set[str], courier_rows) -> dict[str, int]:
    max_group = max(
        (
            row.zone_group_id
            for row in courier_rows
            if row.document_type == "7"
            and row.status == "new"
            and row.zone_group_id is not None
        ),
        default=0,
    )
    return {topic: max_group for topic in users}


def build_test_state_path(test_users_file: str, home: Path | None = None) -> Path:
    state_dir = (home or Path.home()) / ".local" / "state" / "nakladka"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{Path(test_users_file).stem}.db"


async def run_service(
    cfg: SimpleNamespace,
    stop: asyncio.Event | None = None,
    test_users_file: str | None = None,
) -> int:
    stop = stop or asyncio.Event()
    courier_query = load_query(COURIER_QUERY_FILE)
    ready_users_query = load_query(READY_USERS_QUERY_FILE)
    test_mode = test_users_file is not None
    work_today_users_query = (
        None if test_mode else load_query(WORK_TODAY_USERS_QUERY_FILE)
    )
    if test_mode:
        cfg = SimpleNamespace(**vars(cfg))
        cfg.supervisor_topic = cfg.test_supervisor_topic
        cfg.response_topic = cfg.test_response_topic
        cfg.state_file = str(build_test_state_path(test_users_file))
    users = load_users(test_users_file if test_mode else cfg.users_file)
    if test_mode:
        logger.warning(
            "TEST MODE: recipients from %s; supervisor=%s; responses=%s; state=%s",
            test_users_file,
            cfg.supervisor_topic,
            cfg.response_topic,
            cfg.state_file,
        )
    state = open_state(cfg.state_file)
    ntfy = Ntfy(cfg)
    db = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    dispatcher = OrderDispatcher(state)
    latest_orders: list[tuple[int | None, str, int | None]] = []
    latest_busy: set[str] = set()
    latest_work_today: dict[str, int] = {}
    latest_ready_messages: list[tuple[str, str, str, str, str | None]] = []
    latest_ready_targets: dict[int, str] = {}
    previous_ready_targets: dict[int, str] = {}
    snapshot_at = 0.0
    poll_finished = asyncio.Event()
    poll_updated = asyncio.Event()
    force_poll = asyncio.Event()
    dispatch_wakeup = asyncio.Event()
    background_publish_tasks: set[asyncio.Task] = set()

    async def retry_publish(
        topic: str, message: str, title: str, priority: str, click: str | None = None
    ) -> None:
        while not stop.is_set():
            try:
                await ntfy.publish_to(topic, message, title, priority, click)
                return
            except Exception as exc:
                logger.error("Notification to ntfy topic %s failed: %s", topic, exc)
                await _sleep_until(stop, SEND_CYCLE_SECONDS)

    def schedule_publish(
        topic: str, message: str, title: str, priority: str, click: str | None = None
    ) -> None:
        task = asyncio.create_task(retry_publish(topic, message, title, priority, click))
        background_publish_tasks.add(task)
        task.add_done_callback(background_publish_tasks.discard)

    async def poll_loop() -> None:
        nonlocal db, latest_orders, latest_busy, latest_ready_messages
        nonlocal latest_work_today, latest_ready_targets, snapshot_at
        while not stop.is_set():
            try:
                if db is None:
                    db = connect_db(cfg)
                with db.cursor() as cursor:
                    courier_rows = fetch_courier_rows(cursor, courier_query)
                    work_today = (
                        test_mode_work_today(users, courier_rows)
                        if test_mode
                        else fetch_work_today_users(cursor, work_today_users_query, users)
                    )
                    ready_messages: list[tuple[str, str, str, str, str | None]] = []
                    ready_targets: dict[int, str] = {}
                    ready_users: set[str] = set()
                    for row in courier_rows:
                        if row.doc_id is not None:
                            courier_changed(state, row.doc_id, row.courier_id, row.status, row.user_name)
                        if (
                            row.doc_id is not None
                            and row.document_type == "22"
                            and row.courier_id == str(cfg.courier_id)
                            and row.status in {"new", "in_progress"}
                            and row.item_count == 0
                            and row.number
                        ):
                            if test_mode:
                                test_user = next(
                                    (topic for topic in sorted(users) if topic not in ready_users),
                                    None,
                                )
                                ready_text = row.number
                                click_url = ORDER_URL.format(row.doc_id)
                                if test_user:
                                    ready_users.add(test_user)
                                    ready_targets[row.doc_id] = test_user
                                    ready_payload = json.dumps(
                                        {
                                            "type": "ready",
                                            "order_id": row.doc_id,
                                            "text": ready_text,
                                            "click_url": click_url,
                                        },
                                        ensure_ascii=False,
                                    )
                                    ready_messages.append(
                                        (test_user, ready_payload, "Gotowe do wydania", "max", click_url)
                                    )
                                ready_messages.append(
                                    (cfg.supervisor_topic, ready_text, "Gotowe do wydania", "max", click_url)
                                )
                                continue
                            top_users = fetch_top_ready_user(cursor, ready_users_query, row.number)
                            top_user = top_users[0] if top_users else None
                            ready_text = row.number
                            click_url = ORDER_URL.format(top_user[2]) if top_user else None
                            if top_user:
                                ready_users.add(top_user[0])
                                ready_text += "\n" + "\n".join(
                                    f"{login} ({count})" for login, count, _document_id in top_users
                                )
                                if top_user[0] in users:
                                    ready_targets[row.doc_id] = top_user[0]
                                    ready_payload = json.dumps(
                                        {
                                            "type": "ready",
                                            "order_id": row.doc_id,
                                            "text": ready_text,
                                            "click_url": click_url,
                                        },
                                        ensure_ascii=False,
                                    )
                                    ready_messages.append(
                                        (top_user[0], ready_payload, "Gotowe do wydania", "max", click_url)
                                    )
                            ready_messages.append(
                                (cfg.supervisor_topic, ready_text, "Gotowe do wydania", "max", click_url)
                            )
                    latest_orders = sorted(
                        [
                            (row.doc_id, row.number, row.zone_group_id)
                            for row in courier_rows
                            if row.document_type == "7"
                            and row.status == "new"
                            and row.number
                            and row.zone_group_id is not None
                        ],
                        key=lambda order: (order[0] is None, order[0] or 0, order[1]),
                    )
                    latest_work_today = work_today
                    latest_busy = (
                        set(ready_users)
                        if test_mode
                        else {
                            row.user_name
                            for row in courier_rows
                            if row.user_name
                            and row.status == "in_progress"
                            and (
                                row.document_type == "7"
                                or (row.document_type == "22" and row.item_count > 0)
                            )
                        }
                        | ready_users
                    )
                    latest_ready_messages = ready_messages
                    latest_ready_targets = ready_targets

                snapshot_at = time.monotonic()
                free_recipients = sum(
                    login in latest_work_today and login not in latest_busy
                    for login in users
                )
                working_by_group = Counter(latest_work_today.values())
                working_groups = ", ".join(
                    f"{group}:{working_by_group[group]}" for group in sorted(working_by_group)
                ) or "brak"
                logger.info(
                    "Poll OK; new orders: %d, busy: %d, working: %s, free: %d",
                    len(latest_orders), len(latest_busy), working_groups, free_recipients,
                )
                poll_finished.set()
                poll_updated.set()
                dispatch_wakeup.set()
                try:
                    await asyncio.wait_for(force_poll.wait(), timeout=cfg.poll_interval)
                except asyncio.TimeoutError:
                    pass
                force_poll.clear()
            except DbError as exc:
                logger.error("MSSQL error: %s", exc)
                if db is not None:
                    db.close()
                db = None
                await _sleep_until(stop, RECONNECT_DELAY)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in poll loop")
                await _sleep_until(stop, RECONNECT_DELAY)

    async def announce_loop() -> None:
        nonlocal previous_ready_targets
        while not stop.is_set():
            if cfg.announce_interval == 0:
                await poll_finished.wait()
                poll_finished.clear()
            else:
                await _sleep_until(stop, cfg.announce_interval)
            if stop.is_set():
                break

            messages = list(latest_ready_messages)
            if latest_orders:
                supervisor_orders = [
                    message
                    for message in build_new_order_messages(
                        latest_orders, users, latest_work_today, latest_busy, cfg.supervisor_topic
                    )
                    if message[0] == cfg.supervisor_topic
                ]
                messages.extend(supervisor_orders)
            messages = merge_supervisor_messages(messages, cfg.supervisor_topic)
            if cfg.send_text and messages:
                await _send_batch(ntfy, messages)

            resolved_ready = {
                order_id
                for order_id, previous_user in previous_ready_targets.items()
                if latest_ready_targets.get(order_id) != previous_user
            }
            for order_id in resolved_ready:
                user_topic = previous_ready_targets[order_id]
                control = json.dumps({"type": "dismiss", "order_id": order_id})
                schedule_publish(user_topic, control, "Zamówienie zamknięte", "min")
            previous_ready_targets = dict(latest_ready_targets)

    async def publish_dispatch_action(action: DispatchAction) -> None:
        if action.kind == "dismiss" and action.user_topic:
            payload = json.dumps({"type": "dismiss", "order_id": action.order_id})
            schedule_publish(action.user_topic, payload, "Oferta zamknięta", "min")
            return
        if action.kind == "round_complete":
            text = f"Zamówienie {action.order_number} przeszło całą kolejkę (runda {action.round_number})."
            schedule_publish(cfg.supervisor_topic, text, "Kolejka zamówienia", "high")
            return
        if action.kind == "offer" and action.user_topic:
            try:
                click_url = ORDER_URL.format(action.order_id)
                payload = json.dumps(
                    {
                        "type": "offer",
                        "order_id": action.order_id,
                        "order_number": action.order_number,
                        "zone_group_id": action.zone_group_id,
                        "text": f"{action.order_number} (grupa: {action.zone_group_id})",
                        "click_url": click_url,
                        "response_topic": cfg.response_topic,
                    },
                    ensure_ascii=False,
                )
                await ntfy.publish_to(
                    action.user_topic, payload, "Nowe zamówienie", "high", click_url
                )
                dispatcher.finish_publish(action, success=True, now=time.time())
            except Exception as exc:
                logger.error("Notification to ntfy topic %s failed: %s", action.user_topic, exc)
                dispatcher.finish_publish(action, success=False, now=time.time())

    async def dispatch_loop() -> None:
        while not stop.is_set():
            actions = dispatcher.advance(
                latest_orders, users, latest_work_today, latest_busy, now=time.time()
            )
            offers = [action for action in actions if action.kind == "offer"]
            for action in actions:
                if action.kind != "offer":
                    await publish_dispatch_action(action)

            if offers:
                if time.monotonic() - snapshot_at > 1:
                    poll_updated.clear()
                    force_poll.set()
                    try:
                        await asyncio.wait_for(poll_updated.wait(), timeout=RECONNECT_DELAY * 2)
                    except asyncio.TimeoutError:
                        logger.error("Fresh MSSQL poll did not complete; delaying order offers")
                        await _sleep_until(stop, 1)
                    continue
                for action in offers:
                    await publish_dispatch_action(action)

            try:
                await asyncio.wait_for(dispatch_wakeup.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            dispatch_wakeup.clear()

    async def response_loop() -> None:
        since = str(int(time.time()))
        while not stop.is_set():
            try:
                messages = await ntfy.poll_messages(cfg.response_topic, since=since, timeout=5)
                for event in messages:
                    message_id = event.get("id")
                    if message_id:
                        since = str(message_id)
                    raw_message = event.get("message")
                    if not isinstance(raw_message, str):
                        continue
                    try:
                        response = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue
                    if response.get("type") != "reject":
                        continue
                    try:
                        order_id = int(response["order_id"])
                        user_topic = str(response["user_topic"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not dispatcher.reject(order_id, user_topic, now=time.time()):
                        logger.info("Ignoring stale rejection for order %s from %s", order_id, user_topic)
                        continue
                    dispatch_wakeup.set()
                    order_number = next(
                        (number for doc_id, number, _group in latest_orders if doc_id == order_id),
                        str(order_id),
                    )
                    dismissal = json.dumps({"type": "dismiss", "order_id": order_id})
                    schedule_publish(user_topic, dismissal, "Oferta odrzucona", "min")
                    schedule_publish(
                        cfg.supervisor_topic,
                        f"Użytkownik {user_topic} odrzucił zamówienie {order_number}.",
                        "Odrzucone zamówienie",
                        "high",
                    )
            except NtfyError as exc:
                logger.error("Response topic poll failed: %s", exc)
                await _sleep_until(stop, 1)

    poll_task = asyncio.create_task(poll_loop())
    announce_task = asyncio.create_task(announce_loop())
    dispatch_task = asyncio.create_task(dispatch_loop())
    response_task = asyncio.create_task(response_loop())
    try:
        await stop.wait()
    finally:
        poll_task.cancel()
        announce_task.cancel()
        dispatch_task.cancel()
        response_task.cancel()
        await asyncio.gather(
            poll_task, announce_task, dispatch_task, response_task, return_exceptions=True
        )
        for task in tuple(background_publish_tasks):
            task.cancel()
        await asyncio.gather(*background_publish_tasks, return_exceptions=True)
        if db is not None:
            db.close()
        state.close()
        await ntfy.close()
    logger.info("Stopped")
    return 0


def test_db(cfg: SimpleNamespace) -> int:
    try:
        db = connect_db(cfg)
        with db.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
        db.close()
        load_query(COURIER_QUERY_FILE)
        load_query(READY_USERS_QUERY_FILE)
        load_users(cfg.users_file)
    except Exception as exc:
        logger.error("DB test FAILED: %s", exc)
        return 1
    logger.info("DB test OK")
    return 0


async def test_ntfy(cfg: SimpleNamespace) -> int:
    topic = cfg.test_topic or cfg.supervisor_topic
    try:
        await Ntfy(cfg).publish_to(
            topic, "Test wiadomości z bota MSSQL", "Test ntfy", "default"
        )
    except NtfyError as exc:
        logger.error("ntfy test FAILED: %s", exc)
        return 1
    return 0


async def test_new_notification(cfg: SimpleNamespace) -> int:
    topic = cfg.test_topic or cfg.supervisor_topic
    ntfy = Ntfy(cfg)
    try:
        await ntfy.publish_to(
            topic,
            "TEST-7",
            "Nowe zamówienie",
            "high",
            ORDER_URL.format("TEST-7"),
        )
    except NtfyError as exc:
        logger.error("New notification test FAILED: %s", exc)
        return 1
    finally:
        await ntfy.close()
    logger.info("New notification test OK: sent to %s", topic)
    return 0


async def test_ready_notification(cfg: SimpleNamespace) -> int:
    topic = cfg.test_topic or cfg.supervisor_topic
    ntfy = Ntfy(cfg)
    try:
        await ntfy.publish_to(topic, "TEST-22", "Gotowe do wydania", "max")
    except NtfyError as exc:
        logger.error("Ready notification test FAILED: %s", exc)
        return 1
    finally:
        await ntfy.close()
    logger.info("Ready notification test OK: sent to %s", topic)
    return 0


async def test_notification(cfg: SimpleNamespace, topic: str, priority: str) -> int:
    """Wyślij ręczne powiadomienie testowe na wskazany topic i z priorytetem."""
    allowed_priorities = {"min", "low", "default", "high", "max", "1", "2", "3", "4", "5"}
    if not topic.strip() or any(ch.isspace() for ch in topic):
        logger.error("Notification test FAILED: topic nie może być pusty ani zawierać spacji")
        return 2
    if priority.lower() not in allowed_priorities:
        logger.error(
            "Notification test FAILED: nieprawidłowy priorytet %r "
            "(dozwolone: min, low, default, high, max lub 1-5)",
            priority,
        )
        return 2

    ntfy = Ntfy(cfg)
    try:
        await ntfy.publish_to(
            topic,
            f"Testowe powiadomienie (priorytet: {priority})",
            "Test ntfy",
            priority.lower(),
        )
    except NtfyError as exc:
        logger.error("Notification test FAILED: %s", exc)
        return 1
    finally:
        await ntfy.close()
    logger.info("Notification test OK: sent to %s with priority %s", topic, priority.lower())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MSSQL -> ntfy")
    parser.add_argument("--test-db", action="store_true")
    parser.add_argument("--test-ntfy", "--test-text", dest="test_ntfy", action="store_true")
    parser.add_argument("--test-new", action="store_true")
    parser.add_argument("--test-ready", action="store_true")
    parser.add_argument(
        "--test-users",
        nargs="?",
        const="testusers.txt",
        metavar="FILE",
        help="uruchom pełną pętlę na odbiorcach z FILE (domyślnie testusers.txt)",
    )
    parser.add_argument(
        "--test-notification",
        nargs=2,
        metavar=("TOPIC", "PRIORITY"),
        help="wyślij testowe powiadomienie: TOPIC PRIORITY (np. moj-topic max)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.test_db:
        return test_db(cfg)
    if args.test_ntfy:
        return asyncio.run(test_ntfy(cfg))
    if args.test_new:
        return asyncio.run(test_new_notification(cfg))
    if args.test_ready:
        return asyncio.run(test_ready_notification(cfg))
    if args.test_notification:
        topic, priority = args.test_notification
        return asyncio.run(test_notification(cfg, topic, priority))
    if args.test_users is not None:
        return asyncio.run(run_service(cfg, test_users_file=args.test_users))
    try:
        return asyncio.run(run_service(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
