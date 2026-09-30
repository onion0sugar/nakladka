"""Publisher and JSON-poll client for a self-hosted ntfy server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib import error, request

logger = logging.getLogger("ntfy")


def _utf8_header(value: str) -> str:
    """Przygotuj UTF-8 dla nagłówka HTTP wysyłanego przez urllib."""
    return value.encode("utf-8").decode("latin-1")


class NtfyError(Exception):
    """Błąd publikacji lub konfiguracji ntfy."""


def _publish_sync(cfg: SimpleNamespace, message: str, click: str | None = None) -> None:
    url = f"{(cfg.ntfy_server or 'https://ntfy.sh').rstrip('/')}/{cfg.ntfy_topic.lstrip('/')}"
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": _utf8_header(cfg.ntfy_title),
        "Priority": cfg.ntfy_priority,
    }
    if cfg.ntfy_tags:
        headers["Tags"] = cfg.ntfy_tags
    if click:
        headers["Click"] = click
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    req = request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise NtfyError(f"ntfy returned HTTP {response.status}")
            response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise NtfyError(f"ntfy returned HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise NtfyError(f"ntfy connection failed: {exc}") from exc


def _poll_sync(
    cfg: SimpleNamespace,
    topic: str,
    since: str,
    timeout: float = 5,
) -> list[dict[str, object]]:
    base_url = (cfg.ntfy_server or "https://ntfy.sh").rstrip("/")
    url = f"{base_url}/{topic.lstrip('/')}/json?{urlencode({'poll': '1', 'since': since})}"
    headers = {"Accept": "application/json"}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise NtfyError(f"ntfy returned HTTP {response.status}")
            body = response.read().decode("utf-8").strip()
    except error.HTTPError as exc:
        if exc.code == 204:
            return []
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise NtfyError(f"ntfy returned HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise NtfyError(f"ntfy connection failed: {exc}") from exc

    if not body:
        return []

    messages: list[dict[str, object]] = []
    for line in body.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NtfyError(f"ntfy returned invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            messages.append(value)
        elif isinstance(value, list):
            messages.extend(item for item in value if isinstance(item, dict))
    return messages


class Ntfy:
    """Asynchroniczny interfejs publikacji do jednego topicu ntfy."""

    def __init__(self, cfg: SimpleNamespace):
        if getattr(cfg, "ntfy_topic", "") and any(ch.isspace() for ch in cfg.ntfy_topic):
            raise NtfyError("NTFY_TOPIC nie może zawierać spacji")
        self._cfg = cfg

    async def publish_to(self, topic: str, message: str, title: str | None = None, priority: str | None = None, click: str | None = None) -> None:
        cfg = SimpleNamespace(**vars(self._cfg))
        cfg.ntfy_topic = topic
        if title:
            cfg.ntfy_title = title
        if priority:
            cfg.ntfy_priority = priority
        await asyncio.to_thread(_publish_sync, cfg, message, click)
        logger.info("Notification sent to ntfy topic %s", topic)

    async def publish(self, message: str) -> None:
        await self.publish_to(self._cfg.ntfy_topic, message)

    async def poll_messages(
        self, topic: str, since: str = "now", timeout: float = 5
    ) -> list[dict[str, object]]:
        if not topic or any(ch.isspace() for ch in topic):
            raise NtfyError("ntfy topic nie może być pusty ani zawierać spacji")
        if since == "now":
            since = str(int(time.time()))
        return await asyncio.to_thread(_poll_sync, self._cfg, topic, since, timeout)

    async def close(self) -> None:
        """Zachowaj jednolity cykl życia adaptera z innymi kanałami."""
        return None
