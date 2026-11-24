from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import aranet_to_mqtt


def test_main_exits_before_connecting_without_a_device_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aranet_to_mqtt, "ARANET_MAC", "")

    with (
        patch("aranet_to_mqtt.connect_mqtt") as connect,
        pytest.raises(SystemExit) as exc_info,
    ):
        aranet_to_mqtt.main()

    assert exc_info.value.code == 1
    connect.assert_not_called()


def test_main_stops_the_session_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aranet_to_mqtt, "ARANET_MAC", "AA:BB:CC:DD:EE:FF")
    client = MagicMock()

    with (
        patch("aranet_to_mqtt.connect_mqtt", return_value=client),
        patch("aranet_to_mqtt._run_poll_loop", new_callable=AsyncMock),
        patch("aranet_to_mqtt.signal.signal"),
    ):
        aranet_to_mqtt.main()

    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()


def test_main_stops_the_session_when_the_poll_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aranet_to_mqtt, "ARANET_MAC", "AA:BB:CC:DD:EE:FF")
    client = MagicMock()

    async def fail(_client: object) -> None:
        raise RuntimeError("poll failed")

    with (
        patch("aranet_to_mqtt.connect_mqtt", return_value=client),
        patch("aranet_to_mqtt._run_poll_loop", fail),
        patch("aranet_to_mqtt.signal.signal"),
        pytest.raises(RuntimeError, match="poll failed"),
    ):
        aranet_to_mqtt.main()

    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
