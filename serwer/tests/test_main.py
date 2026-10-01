import asyncio
import logging
from types import SimpleNamespace

import main
from state import acknowledge_ready_order, clear_ready_acknowledgement, load_ready_acknowledgements, open_state


def test_ready_acknowledgement_is_persisted_until_cleared():
    connection = open_state(":memory:")

    acknowledge_ready_order(connection, 42, "picker")
    assert load_ready_acknowledgements(connection) == {42: "picker"}

    clear_ready_acknowledgement(connection, 42)
    assert load_ready_acknowledgements(connection) == {}
    connection.close()


def test_send_batch_publishes_notifications_sequentially(caplog):
    caplog.set_level(logging.INFO, logger="bot")
    sent = []
    active = 0
    max_active = 0

    class FakeNtfy:
        async def publish_to(self, *message):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            sent.append(message)
            active -= 1

    messages = [
        ("user1", "first", "title", "high", None),
        ("user2", "second", "title", "high", None),
        ("user3", "third", "title", "high", None),
    ]

    asyncio.run(main._send_batch(FakeNtfy(), messages))

    assert sent == messages
    assert max_active == 1
    assert not any(
        record.getMessage().startswith("Sending notification to ntfy topic ")
        for record in caplog.records
    )


def test_test_notification_sends_topic_and_priority(monkeypatch):
    sent = []

    class FakeNtfy:
        def __init__(self, cfg):
            pass

        async def publish_to(self, *args):
            sent.append(args)

        async def close(self):
            pass

    monkeypatch.setattr(main, "Ntfy", FakeNtfy)
    result = asyncio.run(main.test_notification(SimpleNamespace(), "test-topic", "MAX"))

    assert result == 0
    assert sent == [("test-topic", "Testowe powiadomienie (priorytet: MAX)", "Test ntfy", "max")]


def test_test_ntfy_uses_supervisor_topic_when_test_topic_is_empty(monkeypatch):
    sent = []

    class FakeNtfy:
        def __init__(self, cfg):
            pass

        async def publish_to(self, *args):
            sent.append(args)

    monkeypatch.setattr(main, "Ntfy", FakeNtfy)
    cfg = SimpleNamespace(test_topic="", supervisor_topic="supervisor")

    result = asyncio.run(main.test_ntfy(cfg))

    assert result == 0
    assert sent == [("supervisor", "Test wiadomości z bota MSSQL", "Test ntfy", "default")]


def test_test_mode_work_today_makes_file_users_available_for_all_order_groups():
    rows = [
        SimpleNamespace(document_type="7", status="new", zone_group_id=1),
        SimpleNamespace(document_type="7", status="new", zone_group_id=4),
        SimpleNamespace(document_type="7", status="in_progress", zone_group_id=8),
        SimpleNamespace(document_type="22", status="new", zone_group_id=9),
    ]

    assert main.test_mode_work_today({"test-a", "test-b"}, rows) == {
        "test-a": 4,
        "test-b": 4,
    }


def test_test_mode_state_is_created_under_user_home(tmp_path):
    state_path = main.build_test_state_path("/opt/nakladka/serwer/testusers.txt", tmp_path)

    assert state_path == tmp_path / ".local" / "state" / "nakladka" / "testusers.db"
    assert state_path.parent.is_dir()


def test_test_notification_rejects_invalid_priority():
    result = asyncio.run(main.test_notification(SimpleNamespace(), "test-topic", "urgent"))

    assert result == 2


def test_response_poll_retry_delay_backs_off_up_to_one_minute():
    delays = []
    delay = main.RESPONSE_POLL_INTERVAL
    for _ in range(6):
        delay = main.next_response_retry_delay(delay)
        delays.append(delay)

    assert delays == [10.0, 20.0, 40.0, 60.0, 60.0, 60.0]
