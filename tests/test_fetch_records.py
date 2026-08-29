from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _fail_then_succeed(
    first: BaseException,
    history: MagicMock,
) -> Callable[..., Any]:
    calls = {"n": 0}

    async def fake_fetch(*_args: object, **_kwargs: object) -> MagicMock:
        calls["n"] += 1
        if calls["n"] == 1:
            raise first
        return history

    return fake_fetch


def _run_fetch(
    mac: str = "AA:BB:CC:DD:EE:FF",
    since: datetime | None = None,
) -> list[aranet4.client.RecordItem]:
    return asyncio.run(aranet_to_mqtt.fetch_records(mac, since))


def test_format_fetch_error_empty_message_uses_type_name() -> None:
    assert aranet_to_mqtt._format_fetch_error(aranet4.client.Aranet4Error("")) == "Aranet4Error (no message)"


def test_format_fetch_error_dbus_eof_is_explicit() -> None:
    assert aranet_to_mqtt._format_fetch_error(EOFError()) == "D-Bus connection lost (EOFError)"


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
    fake_fetch = AsyncMock(return_value=history)
    with patch("aranet_to_mqtt._fetch_all_records_async", fake_fetch):
        records = _run_fetch()
    assert records == history.value
    fake_fetch.assert_awaited_once()


def test_fetch_records_retries_after_timeout_then_succeeds() -> None:
    history = _mock_history()
    with (
        patch(
            "aranet_to_mqtt._fetch_all_records_async",
            _fail_then_succeed(TimeoutError(), history),
        ),
        patch("aranet_to_mqtt._reset_bleak_bluez_manager") as mock_reset,
        patch("aranet_to_mqtt.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("aranet_to_mqtt.CONNECT_RETRIES", 3),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
    ):
        records = _run_fetch()
    assert records == history.value
    mock_reset.assert_called_once()
    mock_sleep.assert_awaited_once_with(0)


def test_fetch_records_raises_after_all_timeouts() -> None:
    with (
        patch(
            "aranet_to_mqtt._fetch_all_records_async",
            AsyncMock(side_effect=TimeoutError()),
        ),
        patch("aranet_to_mqtt._reset_bleak_bluez_manager") as mock_reset,
        patch("aranet_to_mqtt.asyncio.sleep", new_callable=AsyncMock),
        patch("aranet_to_mqtt.CONNECT_RETRIES", 2),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _run_fetch()
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert mock_reset.call_count == 2


def test_fetch_records_retries_after_ble_error() -> None:
    history = _mock_history()
    ble_error = aranet4.client.Aranet4Error("device not found")
    with (
        patch(
            "aranet_to_mqtt._fetch_all_records_async",
            _fail_then_succeed(ble_error, history),
        ),
        patch("aranet_to_mqtt._reset_bleak_bluez_manager") as mock_reset,
        patch("aranet_to_mqtt.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("aranet_to_mqtt.CONNECT_RETRIES", 3),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
    ):
        records = _run_fetch()
    assert records == history.value
    mock_reset.assert_called_once()
    mock_sleep.assert_awaited_once_with(0)


def test_fetch_records_retries_after_dbus_eof_then_succeeds() -> None:
    history = _mock_history()
    with (
        patch(
            "aranet_to_mqtt._fetch_all_records_async",
            _fail_then_succeed(EOFError(), history),
        ),
        patch("aranet_to_mqtt._reset_bleak_bluez_manager") as mock_reset,
        patch("aranet_to_mqtt.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("aranet_to_mqtt.CONNECT_RETRIES", 3),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
    ):
        records = _run_fetch()
    assert records == history.value
    mock_reset.assert_called_once()
    mock_sleep.assert_awaited_once_with(0)


def test_fetch_records_raises_after_all_dbus_eofs() -> None:
    with (
        patch(
            "aranet_to_mqtt._fetch_all_records_async",
            AsyncMock(side_effect=EOFError()),
        ),
        patch("aranet_to_mqtt._reset_bleak_bluez_manager") as mock_reset,
        patch("aranet_to_mqtt.asyncio.sleep", new_callable=AsyncMock),
        patch("aranet_to_mqtt.CONNECT_RETRIES", 2),
        patch("aranet_to_mqtt.CONNECT_RETRY_DELAY", 0),
        pytest.raises(RuntimeError) as exc_info,
    ):
        _run_fetch()
    assert isinstance(exc_info.value.__cause__, EOFError)
    assert mock_reset.call_count == 2


def test_fetch_records_since_adds_start_filter() -> None:
    since = datetime(2026, 1, 1, 12, 0, 0)
    history = _mock_history()
    captured: dict[str, object] = {}

    async def fake_fetch(mac: str, entry_filter: dict[str, object]) -> MagicMock:
        captured["mac"] = mac
        captured["entry_filter"] = entry_filter.copy()
        return history

    with patch("aranet_to_mqtt._fetch_all_records_async", fake_fetch):
        asyncio.run(aranet_to_mqtt.fetch_records("AA:BB:CC:DD:EE:FF", since))

    assert captured["mac"] == "AA:BB:CC:DD:EE:FF"
    assert captured["entry_filter"]["start"] == since + aranet_to_mqtt.timedelta(seconds=1)


def test_reset_bleak_bluez_manager_disconnects_cached_bus() -> None:
    async def exercise() -> None:
        from bleak.backends.bluezdbus.manager import _global_instances

        loop = asyncio.get_running_loop()
        bus = MagicMock()
        manager = MagicMock()
        manager._bus = bus
        _global_instances[loop] = manager
        aranet_to_mqtt._reset_bleak_bluez_manager()
        assert loop not in _global_instances
        bus.disconnect.assert_called_once()

    asyncio.run(exercise())


def test_reset_bleak_bluez_manager_ignores_disconnect_errors() -> None:
    async def exercise() -> None:
        from bleak.backends.bluezdbus.manager import _global_instances

        loop = asyncio.get_running_loop()
        bus = MagicMock()
        bus.disconnect.side_effect = OSError("already closed")
        manager = MagicMock()
        manager._bus = bus
        _global_instances[loop] = manager
        aranet_to_mqtt._reset_bleak_bluez_manager()
        assert loop not in _global_instances

    asyncio.run(exercise())
