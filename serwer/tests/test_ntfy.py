import asyncio
import time
from types import SimpleNamespace

import ntfy


def test_poll_sync_reads_json_lines_and_authenticates(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"id":"m1","event":"message"}\n{"id":"m2","event":"message"}\n'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(ntfy.request, "urlopen", fake_urlopen)
    messages = ntfy._poll_sync(
        SimpleNamespace(ntfy_server="https://ntfy.local", ntfy_token="secret"),
        "order-responses",
        "previous-id",
        timeout=4,
    )

    assert [message["id"] for message in messages] == ["m1", "m2"]
    assert "poll=1" in captured["url"]
    assert "since=previous-id" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 4


def test_poll_messages_converts_default_now_to_unix_timestamp(monkeypatch):
    captured = {}

    def fake_poll_sync(cfg, topic, since, timeout):
        captured["topic"] = topic
        captured["since"] = since
        return []

    monkeypatch.setattr(ntfy, "_poll_sync", fake_poll_sync)
    client = ntfy.Ntfy(SimpleNamespace(ntfy_server="https://ntfy.local", ntfy_token=""))
    before = int(time.time())

    asyncio.run(client.poll_messages("order-responses"))

    assert captured["topic"] == "order-responses"
    assert int(captured["since"]) >= before