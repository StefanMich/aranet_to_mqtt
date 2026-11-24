from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import aranet_to_mqtt


def _record(
    ts: datetime,
    *,
    temperature: float = 20.0,
    humidity: float = 50.0,
    pressure: float = 1013.0,
    radon: float = 100.0,
) -> MagicMock:
    rec = MagicMock()
    rec.date = ts
    rec.temperature = temperature
    rec.humidity = humidity
    rec.pressure = pressure
    rec.radon_concentration = radon
    return rec


def _empty_record(ts: datetime) -> MagicMock:
    sentinel = aranet_to_mqtt.NO_DATA_SENTINEL
    return _record(
        ts,
        temperature=sentinel,
        humidity=sentinel,
        pressure=sentinel,
        radon=sentinel,
    )


def _mock_publish_client() -> MagicMock:
    client = MagicMock()
    info = MagicMock()
    info.is_published.return_value = True
    client.publish.return_value = info
    return client


def test_publish_records_returns_last_published_not_last_fetched() -> None:
    ts1 = datetime(2026, 1, 1, 12, 0, 0)
    ts2 = datetime(2026, 1, 1, 12, 5, 0)
    ts3 = datetime(2026, 1, 1, 12, 10, 0)
    records = [
        _record(ts1),
        _empty_record(ts2),
        _record(ts3),
    ]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result == ts3
    assert client.publish.call_count == 2


def test_publish_records_does_not_advance_past_unpublished_tail() -> None:
    ts1 = datetime(2026, 1, 1, 12, 0, 0)
    ts2 = datetime(2026, 1, 1, 12, 5, 0)
    records = [
        _record(ts1),
        _empty_record(ts2),
    ]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result == ts1
    assert client.publish.call_count == 1


def test_publish_records_checkpoint_uses_last_published() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = [_record(base + timedelta(minutes=i)) for i in range(100)]
    records.append(_empty_record(base + timedelta(hours=1)))
    client = _mock_publish_client()

    with patch("aranet_to_mqtt.save_state") as mock_save_state:
        aranet_to_mqtt.publish_records(client, records)

    mock_save_state.assert_called_once_with(base + timedelta(minutes=99))


def test_publish_records_all_empty_returns_none() -> None:
    ts = datetime(2026, 1, 1, 12, 0, 0)
    records = [_empty_record(ts)]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result is None
    client.publish.assert_not_called()


def test_publish_records_publishes_partial_readings_as_null() -> None:
    ts = datetime(2026, 1, 1, 12, 0, 0)
    records = [_record(ts, radon=aranet_to_mqtt.NO_DATA_SENTINEL)]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result == ts
    payload = json.loads(client.publish.call_args.args[1])
    assert payload["radon"] is None
    assert payload["temperature"] == 20.0
    assert payload["humidity"] == 50.0
    assert payload["pressure"] == 1013.0


def test_publish_records_payload_keeps_zero_on_the_device_topic() -> None:
    ts = datetime(2026, 1, 1, 12, 0, 0)
    records = [
        _record(
            ts,
            temperature=0.0,
            humidity=0,
            pressure=0,
            radon=0,
        )
    ]
    client = _mock_publish_client()

    with (
        patch("aranet_to_mqtt.MQTT_TOPIC_PREFIX", "sensors"),
        patch("aranet_to_mqtt.DEVICE_NAME", "lab"),
    ):
        result = aranet_to_mqtt.publish_records(client, records)

    assert result == ts
    args, kwargs = client.publish.call_args
    assert args[0] == "sensors/lab/measurement"
    assert kwargs["qos"] == 1
    assert json.loads(args[1]) == {
        "timestamp": ts.isoformat(),
        "temperature": 0.0,
        "humidity": 0,
        "pressure": 0,
        "radon": 0,
    }


def test_publish_records_checkpoint_counts_published_records() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0)
    real_times = [base + timedelta(minutes=i) for i in range(101)]
    records: list[MagicMock] = [_empty_record(base - timedelta(minutes=i + 1)) for i in range(10)]
    records.extend(_record(ts) for ts in real_times)
    client = _mock_publish_client()

    with patch("aranet_to_mqtt.save_state") as mock_save_state:
        result = aranet_to_mqtt.publish_records(client, records)

    mock_save_state.assert_called_once_with(real_times[99])
    assert result == real_times[100]
    assert client.publish.call_count == 101


def test_publish_timeout_keeps_the_earlier_checkpoint_only() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = [_record(base + timedelta(minutes=i)) for i in range(101)]
    client = MagicMock()
    calls = {"n": 0}

    def publish(*_args: object, **_kwargs: object) -> MagicMock:
        calls["n"] += 1
        info = MagicMock()
        info.is_published.return_value = calls["n"] <= 100
        return info

    client.publish.side_effect = publish

    with (
        patch("aranet_to_mqtt.save_state") as mock_save_state,
        pytest.raises(TimeoutError),
    ):
        aranet_to_mqtt.publish_records(client, records)

    mock_save_state.assert_called_once_with(records[99].date)
