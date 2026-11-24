from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

import aranet_to_mqtt

if TYPE_CHECKING:
    from pathlib import Path


def _use_state(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(aranet_to_mqtt, "STATE_FILE", path)


def test_load_state_missing_file_has_no_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_state(monkeypatch, tmp_path / "state.json")

    assert aranet_to_mqtt.load_state() is None


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "{}",
        '{"last_timestamp": "not-a-date"}',
    ],
)
def test_load_state_unreadable_cursor_starts_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
) -> None:
    state = tmp_path / "state.json"
    state.write_text(contents)
    _use_state(monkeypatch, state)

    assert aranet_to_mqtt.load_state() is None


@pytest.mark.parametrize(
    "ts",
    [
        datetime(2026, 1, 1, 12, 0, 0),
        datetime(2026, 6, 1, 15, 30, tzinfo=UTC),
    ],
)
def test_save_state_round_trips_the_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ts: datetime,
) -> None:
    _use_state(monkeypatch, tmp_path / "state.json")

    aranet_to_mqtt.save_state(ts)

    assert aranet_to_mqtt.load_state() == ts


def test_save_state_writes_a_complete_file_in_a_new_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "data" / "state.json"
    ts = datetime(2026, 1, 1, 12, 0, 0)
    _use_state(monkeypatch, state)

    aranet_to_mqtt.save_state(ts)

    assert json.loads(state.read_text()) == {"last_timestamp": ts.isoformat()}
    assert not state.with_suffix(".tmp").exists()
    assert aranet_to_mqtt.load_state() == ts
