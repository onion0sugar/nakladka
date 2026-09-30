import asyncio
import logging
from types import SimpleNamespace

import main
from main import build_new_order_messages, merge_supervisor_messages


def test_new_orders_are_split_by_zone_group():
    messages = build_new_order_messages(
        [(101, "ZAM-1", 1), (102, "ZAM-2", 2)],
        {"grupa1", "grupa2", "grupa3", "zajety"},
        {"grupa1": 1, "grupa2": 2, "grupa3": 3, "zajety": 3},
        {"zajety"},
        "supervisor",
    )

    recipients_by_order = {
        "ZAM-1": {topic for topic, text, *_ in messages if "ZAM-1" in text},
        "ZAM-2": {topic for topic, text, *_ in messages if "ZAM-2" in text},
    }
    assert recipients_by_order["ZAM-1"] == {"supervisor", "grupa1"}
    assert recipients_by_order["ZAM-2"] == {"supervisor", "grupa2", "grupa3"}
    assert {priority for _topic, _text, _title, priority, _click in messages} == {"high"}


def test_same_zone_group_only_oldest_order_is_sent():
    messages = build_new_order_messages(
        [(101, "STARSZE", 1), (102, "NOWSZE", 1), (103, "INNA-GRUPA", 2)],
        {"grupa1", "grupa2"},
        {"grupa1": 1, "grupa2": 2},
        set(),
        "supervisor",
    )

    texts = [text for _topic, text, *_ in messages]
    assert any("STARSZE" in text for text in texts)
    assert not any("NOWSZE" in text for text in texts)
    assert any("INNA-GRUPA" in text for text in texts)


def test_user_receives_only_highest_eligible_zone_group():
    messages = build_new_order_messages(
        [(101, "GRUPA-1", 1), (102, "GRUPA-3", 3)],
        {"grupa3"},
        {"grupa3": 3},
        set(),
        "supervisor",
    )

    user_texts = [text for topic, text, *_ in messages if topic == "grupa3"]
    assert user_texts == ["GRUPA-3 (grupa: 3)"]


def test_supervisor_receives_all_new_orders_in_one_notification():
    messages = build_new_order_messages(
        [(101, "GRUPA-1", 1), (102, "GRUPA-3", 3)],
        set(),
        {},
        set(),
        "supervisor",
    )

    supervisor_messages = [message for message in messages if message[0] == "supervisor"]
    assert len(supervisor_messages) == 1
    assert "GRUPA-1 (grupa: 1)\nGRUPA-3 (grupa: 3)" == supervisor_messages[0][1]
    assert supervisor_messages[0][4] is None


def test_supervisor_receives_new_and_ready_orders_in_one_notification():
    messages = merge_supervisor_messages(
        [
            ("supervisor", "GOTOWE-1\nklient (2)", "Gotowe do wydania", "max", "url-ready"),
            ("supervisor", "NOWE-1 (grupa: 1)", "Nowe zamówienia", "high", "url-new"),
            ("grupa1", "NOWE-1 (grupa: 1)", "Nowe zamówienie", "high", "url-new"),
        ],
        "supervisor",
    )

    supervisor_messages = [message for message in messages if message[0] == "supervisor"]
    assert supervisor_messages == [
        (
            "supervisor",
            "GOTOWE-1\nklient (2)\n\nNOWE-1 (grupa: 1)",
            "Nowe zamówienia",
            "max",
            None,
        )
    ]


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
