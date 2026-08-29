from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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
        _record(ts2, radon=aranet_to_mqtt.NO_DATA_SENTINEL),
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
        _record(ts2, temperature=aranet_to_mqtt.NO_DATA_SENTINEL),
    ]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result == ts1
    assert client.publish.call_count == 1


def test_publish_records_checkpoint_uses_last_published() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0)
    records = [_record(base + timedelta(minutes=i)) for i in range(100)]
    records.append(_record(base + timedelta(hours=1), radon=aranet_to_mqtt.NO_DATA_SENTINEL))
    client = _mock_publish_client()

    with patch("aranet_to_mqtt.save_state") as mock_save_state:
        aranet_to_mqtt.publish_records(client, records)

    mock_save_state.assert_called_once_with(base + timedelta(minutes=99))


def test_publish_records_all_invalid_returns_none() -> None:
    ts = datetime(2026, 1, 1, 12, 0, 0)
    records = [_record(ts, radon=aranet_to_mqtt.NO_DATA_SENTINEL)]
    client = _mock_publish_client()

    result = aranet_to_mqtt.publish_records(client, records)

    assert result is None
    client.publish.assert_not_called()
