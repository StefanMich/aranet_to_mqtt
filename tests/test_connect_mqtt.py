from __future__ import annotations

import ssl
from unittest.mock import MagicMock, patch

import pytest

import aranet_to_mqtt


def _client() -> MagicMock:
    client = MagicMock()
    client.connect.return_value = None
    return client


def _use_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_HOST", "broker.test")
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_PORT", 1883)
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_USER", "")
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_PASS", "")
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_TLS", False)
    monkeypatch.setattr(aranet_to_mqtt, "DEVICE_NAME", "lab")


def test_connect_mqtt_starts_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_broker(monkeypatch)
    client = _client()

    with patch("aranet_to_mqtt.mqtt.Client", return_value=client) as mock_cls:
        result = aranet_to_mqtt.connect_mqtt()

    mock_cls.assert_called_once_with(
        aranet_to_mqtt.mqtt.CallbackAPIVersion.VERSION2,
        client_id="aranet-lab",
    )
    client.connect.assert_called_once_with("broker.test", 1883, keepalive=60)
    client.username_pw_set.assert_not_called()
    client.tls_set.assert_not_called()
    client.loop_start.assert_called_once()
    assert result is client


@pytest.mark.parametrize(
    ("user", "password", "expected"),
    [
        ("", "", None),
        ("user", "secret", ("user", "secret")),
        ("user", "", ("user", None)),
    ],
)
def test_connect_mqtt_sends_credentials_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    user: str,
    password: str,
    expected: tuple[str, str | None] | None,
) -> None:
    _use_broker(monkeypatch)
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_USER", user)
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_PASS", password)
    client = _client()

    with patch("aranet_to_mqtt.mqtt.Client", return_value=client):
        aranet_to_mqtt.connect_mqtt()

    if expected is None:
        client.username_pw_set.assert_not_called()
    else:
        client.username_pw_set.assert_called_once_with(*expected)


@pytest.mark.parametrize(
    ("tls", "port", "expect_tls"),
    [
        (True, 1883, True),
        (False, 8883, False),
        (None, 8883, True),
        (None, 1883, False),
    ],
)
def test_connect_mqtt_uses_tls_when_configured_or_implied_by_port(
    monkeypatch: pytest.MonkeyPatch,
    tls: bool | None,
    port: int,
    expect_tls: bool,
) -> None:
    _use_broker(monkeypatch)
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_TLS", tls)
    monkeypatch.setattr(aranet_to_mqtt, "MQTT_PORT", port)
    client = _client()

    with patch("aranet_to_mqtt.mqtt.Client", return_value=client):
        aranet_to_mqtt.connect_mqtt()

    if expect_tls:
        client.tls_set.assert_called_once_with(
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    else:
        client.tls_set.assert_not_called()
    client.connect.assert_called_once_with("broker.test", port, keepalive=60)


def test_connect_mqtt_retries_then_starts_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_broker(monkeypatch)
    monkeypatch.setattr(aranet_to_mqtt, "CONNECT_RETRIES", 3)
    monkeypatch.setattr(aranet_to_mqtt, "CONNECT_RETRY_DELAY", 4)
    client = _client()
    client.connect.side_effect = [OSError("down"), OSError("down"), None]

    with (
        patch("aranet_to_mqtt.mqtt.Client", return_value=client),
        patch("aranet_to_mqtt.time.sleep") as sleep,
    ):
        aranet_to_mqtt.connect_mqtt()

    assert client.connect.call_count == 3
    assert sleep.call_count == 2
    sleep.assert_called_with(4)
    client.loop_start.assert_called_once()


def test_connect_mqtt_raises_when_the_broker_stays_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_broker(monkeypatch)
    monkeypatch.setattr(aranet_to_mqtt, "CONNECT_RETRIES", 2)
    monkeypatch.setattr(aranet_to_mqtt, "CONNECT_RETRY_DELAY", 4)
    client = _client()
    client.connect.side_effect = OSError("down")

    with (
        patch("aranet_to_mqtt.mqtt.Client", return_value=client),
        patch("aranet_to_mqtt.time.sleep") as sleep,
        pytest.raises(OSError, match="down"),
    ):
        aranet_to_mqtt.connect_mqtt()

    assert client.connect.call_count == 2
    sleep.assert_called_once_with(4)
    client.loop_start.assert_not_called()
