from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

import aranet_to_mqtt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@pytest.fixture
def cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> datetime:
    initial = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(aranet_to_mqtt, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(aranet_to_mqtt, "_running", True)
    monkeypatch.setattr(aranet_to_mqtt, "ARANET_MAC", "AA:BB:CC:DD:EE:FF")
    aranet_to_mqtt.save_state(initial)
    return initial


def _stop_after(iterations: int) -> Callable[[int], Awaitable[None]]:
    calls = {"n": 0}

    async def fake_sleep(_seconds: int) -> None:
        calls["n"] += 1
        if calls["n"] >= iterations:
            aranet_to_mqtt._running = False

    return fake_sleep


def test_fetch_failure_retries_with_the_same_cursor(cursor: datetime) -> None:
    fetches: list[tuple[str, datetime | None]] = []
    delays: list[int] = []

    async def fake_fetch(mac: str, since: datetime | None) -> list[object]:
        fetches.append((mac, since))
        raise RuntimeError("ble down")

    async def fake_sleep(seconds: int) -> None:
        delays.append(seconds)
        if len(delays) == 2:
            aranet_to_mqtt._running = False

    publish = MagicMock()
    with (
        patch("aranet_to_mqtt.fetch_records", fake_fetch),
        patch("aranet_to_mqtt.publish_records", publish),
        patch("aranet_to_mqtt._sleep", fake_sleep),
    ):
        asyncio.run(aranet_to_mqtt._run_poll_loop(MagicMock()))

    assert fetches == [("AA:BB:CC:DD:EE:FF", cursor)] * 2
    assert aranet_to_mqtt.load_state() == cursor
    publish.assert_not_called()
    assert delays == [aranet_to_mqtt.POLL_INTERVAL] * 2


def test_no_new_records_leaves_the_cursor(cursor: datetime) -> None:
    async def fake_fetch(_mac: str, _since: datetime | None) -> list[object]:
        return []

    publish = MagicMock()
    with (
        patch("aranet_to_mqtt.fetch_records", fake_fetch),
        patch("aranet_to_mqtt.publish_records", publish),
        patch("aranet_to_mqtt._sleep", _stop_after(1)),
    ):
        asyncio.run(aranet_to_mqtt._run_poll_loop(MagicMock()))

    assert aranet_to_mqtt.load_state() == cursor
    publish.assert_not_called()


def test_publish_failure_leaves_the_cursor(cursor: datetime) -> None:
    async def fake_fetch(_mac: str, _since: datetime | None) -> list[object]:
        return [object()]

    def fake_publish(_client: object, _records: list[object]) -> datetime:
        raise TimeoutError("mqtt down")

    with (
        patch("aranet_to_mqtt.fetch_records", fake_fetch),
        patch("aranet_to_mqtt.publish_records", fake_publish),
        patch("aranet_to_mqtt._sleep", _stop_after(1)),
    ):
        asyncio.run(aranet_to_mqtt._run_poll_loop(MagicMock()))

    assert aranet_to_mqtt.load_state() == cursor


def test_empty_batch_leaves_the_cursor(cursor: datetime) -> None:
    async def fake_fetch(_mac: str, _since: datetime | None) -> list[object]:
        return [object()]

    def fake_publish(_client: object, _records: list[object]) -> None:
        return None

    with (
        patch("aranet_to_mqtt.fetch_records", fake_fetch),
        patch("aranet_to_mqtt.publish_records", fake_publish),
        patch("aranet_to_mqtt._sleep", _stop_after(1)),
    ):
        asyncio.run(aranet_to_mqtt._run_poll_loop(MagicMock()))

    assert aranet_to_mqtt.load_state() == cursor


def test_successful_batch_is_the_next_fetch_cursor(cursor: datetime) -> None:
    latest = datetime(2026, 1, 1, 12, 5, 0)
    fetches: list[datetime | None] = []
    delays: list[int] = []

    async def fake_fetch(_mac: str, since: datetime | None) -> list[object]:
        fetches.append(since)
        if len(fetches) == 1:
            return [object()]
        return []

    def fake_publish(_client: object, _records: list[object]) -> datetime:
        return latest

    async def fake_sleep(seconds: int) -> None:
        delays.append(seconds)
        if len(delays) == 2:
            aranet_to_mqtt._running = False

    with (
        patch("aranet_to_mqtt.fetch_records", fake_fetch),
        patch("aranet_to_mqtt.publish_records", fake_publish),
        patch("aranet_to_mqtt._sleep", fake_sleep),
    ):
        asyncio.run(aranet_to_mqtt._run_poll_loop(MagicMock()))

    assert fetches == [cursor, latest]
    assert aranet_to_mqtt.load_state() == latest
    assert delays == [aranet_to_mqtt.POLL_INTERVAL] * 2


def test_shutdown_signal_ends_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(aranet_to_mqtt, "_running", True)

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        aranet_to_mqtt._handle_signal(signal.SIGTERM, None)

    async def exercise() -> None:
        with patch("aranet_to_mqtt.asyncio.sleep", fake_sleep):
            await aranet_to_mqtt._sleep(300)

    asyncio.run(exercise())

    assert delays == [1.0]
    assert aranet_to_mqtt._running is False
