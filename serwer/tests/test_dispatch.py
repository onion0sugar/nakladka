import sqlite3

from dispatch import OrderDispatcher


def make_dispatcher():
    connection = sqlite3.connect(":memory:")
    return connection, OrderDispatcher(connection)


def offer_actions(actions):
    return [action for action in actions if action.kind == "offer"]


def test_offer_expires_at_15_and_next_user_receives_it_at_16_seconds():
    connection, dispatcher = make_dispatcher()
    users = {"user1", "user2"}
    orders = [(1, "ORDER-1", 1)]
    work_today = {"user1": 1, "user2": 1}

    first_offer = offer_actions(
        dispatcher.advance(orders, users, work_today, set(), now=100)
    )[0]
    assert first_offer.user_topic == "user1"
    dispatcher.finish_publish(first_offer, success=True, now=100)

    actions = dispatcher.advance(orders, users, work_today, set(), now=115)
    assert [action.kind for action in actions] == ["dismiss"]
    assert dispatcher.busy_users() == set()

    second_offer = offer_actions(
        dispatcher.advance(orders, users, work_today, set(), now=116)
    )[0]
    assert second_offer.user_topic == "user2"
    connection.close()


def test_rejection_immediately_offers_next_user():
    connection, dispatcher = make_dispatcher()
    users = {"user1", "user2"}
    work_today = {"user1": 1, "user2": 1}
    first = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=10)
    )[0]
    dispatcher.finish_publish(first, success=True, now=10)

    assert dispatcher.reject(1, "user1", now=11)
    second = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=11)
    )[0]
    assert second.user_topic == "user2"
    assert not dispatcher.reject(1, "user1", now=12)
    connection.close()


def test_parallel_orders_lock_distinct_users():
    connection, dispatcher = make_dispatcher()
    actions = dispatcher.advance(
        [(1, "ORDER-1", 1), (2, "ORDER-2", 1)],
        {"user1", "user2"},
        {"user1": 1, "user2": 1},
        set(),
        now=50,
    )
    offers = offer_actions(actions)
    assert len(offers) == 2
    assert {action.user_topic for action in offers} == {"user1", "user2"}
    connection.close()


def test_no_free_users_waits_without_completing_round():
    connection, dispatcher = make_dispatcher()
    actions = dispatcher.advance(
        [(1, "ORDER-1", 1)],
        {"user1"},
        {"user1": 1},
        {"user1"},
        now=50,
    )
    assert actions == []
    connection.close()


def test_failed_publish_retries_same_user_after_next_send_cycle():
    connection, dispatcher = make_dispatcher()
    users = {"user1", "user2"}
    work_today = {"user1": 1, "user2": 1}
    first = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=20)
    )[0]
    dispatcher.finish_publish(first, success=False, now=20)

    assert dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=35) == []
    retry = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=36)
    )[0]
    assert retry.user_topic == first.user_topic
    connection.close()


def test_retry_reselects_user_when_fresh_poll_removes_group_eligibility():
    connection, dispatcher = make_dispatcher()
    first = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 2)], {"old-user"}, {"old-user": 2}, set(), now=10)
    )[0]

    retry = offer_actions(
        dispatcher.advance(
            [(1, "ORDER-1", 2)],
            {"old-user", "new-user"},
            {"new-user": 2},
            set(),
            now=11,
        )
    )[0]

    assert retry.user_topic == "new-user"
    connection.close()


def test_retry_reselects_user_when_fresh_poll_marks_candidate_busy():
    connection, dispatcher = make_dispatcher()
    first = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 2)], {"old-user"}, {"old-user": 2}, set(), now=10)
    )[0]

    retry = offer_actions(
        dispatcher.advance(
            [(1, "ORDER-1", 2)],
            {"old-user", "new-user"},
            {"old-user": 2, "new-user": 2},
            {"old-user"},
            now=11,
        )
    )[0]

    assert retry.user_topic == "new-user"
    connection.close()


def test_full_round_emits_supervisor_action_then_starts_a_new_round(monkeypatch):
    connection, dispatcher = make_dispatcher()
    monkeypatch.setattr("dispatch.random.shuffle", lambda users: users.reverse())
    users = {"user1", "user2"}
    work_today = {"user1": 1, "user2": 1}

    first = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=10)
    )[0]
    dispatcher.finish_publish(first, success=True, now=10)
    assert dispatcher.reject(1, first.user_topic, now=11)

    second = offer_actions(
        dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=11)
    )[0]
    dispatcher.finish_publish(second, success=True, now=11)
    assert dispatcher.reject(1, second.user_topic, now=12)

    actions = dispatcher.advance([(1, "ORDER-1", 1)], users, work_today, set(), now=12)
    assert actions[0].kind == "round_complete"
    assert actions[0].round_number == 1
    assert offer_actions(actions)[0].user_topic == "user2"
    connection.close()


def test_order_disappearing_from_new_list_dismisses_active_overlay():
    connection, dispatcher = make_dispatcher()
    first = offer_actions(
        dispatcher.advance(
            [(1, "ORDER-1", 1)], {"user1"}, {"user1": 1}, set(), now=10
        )
    )[0]
    dispatcher.finish_publish(first, success=True, now=10)

    actions = dispatcher.advance([], {"user1"}, {"user1": 1}, set(), now=11)
    assert [(action.kind, action.user_topic) for action in actions] == [("dismiss", "user1")]
    assert dispatcher.busy_users() == set()
    connection.close()