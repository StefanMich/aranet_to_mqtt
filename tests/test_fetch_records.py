from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import aranet4.client
import pytest

import aranet_to_mqtt

if TYPE_CHECKING:
    from collections.abc import Callable


def _mock_history(record_count: int = 1) -> MagicMock:
    history = MagicMock()
    history.value = [MagicMock()] * record_count
    history.records_on_device = record_count
    return history


def _close_coro(coro: object) -> None:
    if asyncio.iscoroutine(coro):
        coro.close()


def _mock_run(
    handler: Callable[[object], Any],
) -> Callable[[object], Any]:
    def runner(coro: object) -> Any:
        _close_coro(coro)
        return handler(coro)

    return runner


def test_format_fetch_error_empty_message_uses_type_name() -> None:
    assert aranet_to_mqtt._format_fetch_error(aranet4.client.Aranet4Error("")) == "Aranet4Error (no message)"


def test_fetch_all_records_async_times_out_when_all_records_is_slow() -> None:
    async def slow_records(*_args: object, **_kwargs: object) -> MagicMock:
        await asyncio.sleep(999)
        return _mock_history()

    async def exercise() -> None:
        with (
            patch("aranet_to_mqtt.aranet4.client._all_records", slow_records),
            patch("aranet_to_mqtt.BLE_FETCH_TIMEOUT", 0.05),
        ):
            await aranet_to_mqtt._fetch_all_records_async("AA:BB:CC:DD:EE:FF", {})

    with pytest.raises(TimeoutError):
        asyncio.run(exercise())


def test_fetch_records_returns_on_first_success() -> None:
    history = _mock_history(2)
    mock_run = MagicMock(side_effect=_mock_run(lambda _coro: history))
    with patch("aranet_to_mqtt.asyncio.run", mock_run):
        records = aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", None)
    assert records == history.value
    mock_run.assert_called_once()


def test_fetch_records_retries_after_timeout_then_succeeds() -> None:
    history = _mock_history()
    effects: list[object] = [TimeoutError(), history]
    calls = 0

    def handler(_coro: object) -> object:
        nonlocal calls
        effect = effects[calls]
        calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    mock_run = MagicMock(side_effect=_mock_run(handler))
    with (
        patch("aranet_to_mqtt.asyncio.run", mock_run),
        patch("aranet_to_mqtt.time.sleep") as mock_sleep,
        patch("aranet_to_mqtt.CONNECT_RETRIES", 3),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
    ):
        records = aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", None)
    assert records == history.value
    assert mock_run.call_count == 2
    mock_sleep.assert_called_once_with(0)


def test_fetch_records_raises_after_all_timeouts() -> None:
    def handler(_coro: object) -> object:
        raise TimeoutError

    with (
        patch("aranet_to_mqtt.asyncio.run", _mock_run(handler)),
        patch("aranet_to_mqtt.time.sleep"),
        patch("aranet_to_mqtt.CONNECT_RETRIES", 2),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
        pytest.raises(RuntimeError) as exc_info,
    ):
        aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", None)
    assert isinstance(exc_info.value.__cause__, TimeoutError)


def test_fetch_records_retries_after_ble_error() -> None:
    history = _mock_history()
    ble_error = aranet4.client.Aranet4Error("device not found")
    effects: list[object] = [ble_error, history]
    calls = 0

    def handler(_coro: object) -> object:
        nonlocal calls
        effect = effects[calls]
        calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    mock_run = MagicMock(side_effect=_mock_run(handler))
    with (
        patch("aranet_to_mqtt.asyncio.run", mock_run),
        patch("aranet_to_mqtt.time.sleep") as mock_sleep,
        patch("aranet_to_mqtt.CONNECT_RETRIES", 3),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
    ):
        records = aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", None)
    assert records == history.value
    assert mock_run.call_count == 2
    mock_sleep.assert_called_once_with(0)


def test_fetch_records_since_adds_start_filter() -> None:
    since = datetime(2026, 1, 1, 12, 0, 0)
    history = _mock_history()
    captured: dict[str, object] = {}

    async def fake_fetch(mac: str, entry_filter: dict[str, object]) -> MagicMock:
        captured["mac"] = mac
        captured["entry_filter"] = entry_filter.copy()
        return history

    with (
        patch("aranet_to_mqtt._fetch_all_records_async", fake_fetch),
        patch("aranet_to_mqtt.asyncio.run", wraps=asyncio.run),
    ):
        aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", since)

    assert captured["mac"] == "AA:BB:CC:DD:EE:FF"
    assert captured["entry_filter"]["start"] == since + aranet_to_mqtt.timedelta(seconds=1)
