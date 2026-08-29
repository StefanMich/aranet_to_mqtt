#!/usr/bin/env python3
"""Aranet RN+ to MQTT bridge.

Periodically fetches historical records from an Aranet RN+ sensor over
Bluetooth and publishes them to an MQTT broker.  Persists sync progress
to a JSON state file so that restarts do not re-send old data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import ssl
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aranet4
import paho.mqtt.client as mqtt

log = logging.getLogger("aranet_to_mqtt")

ARANET_MAC: str = os.environ.get("ARANET_MAC", "")
MQTT_HOST: str = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT: int = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER: str = os.environ.get("MQTT_USER", "")
MQTT_PASS: str = os.environ.get("MQTT_PASS", "")
_tls_raw: str = os.environ.get("MQTT_TLS", "").lower()
MQTT_TLS: bool | None = (
    True if _tls_raw in ("1", "true", "yes") else False if _tls_raw in ("0", "false", "no") else None
)
MQTT_TOPIC_PREFIX: str = os.environ.get("MQTT_TOPIC_PREFIX", "aranet")
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "300"))
STATE_FILE: Path = Path(os.environ.get("STATE_FILE", "/data/state.json"))
DEVICE_NAME: str = os.environ.get("DEVICE_NAME", "rn_plus")
PUBLISH_TIMEOUT: int = int(os.environ.get("PUBLISH_TIMEOUT", "30"))
CONNECT_RETRIES: int = int(os.environ.get("CONNECT_RETRIES", "5"))
CONNECT_RETRY_DELAY: int = int(os.environ.get("CONNECT_RETRY_DELAY", "10"))
BLE_FETCH_TIMEOUT: int = int(os.environ.get("BLE_FETCH_TIMEOUT", "120"))
_DBUS_TRANSPORT_ERRORS = (EOFError, BrokenPipeError, ConnectionResetError)

_running = True


def _handle_signal(sig: int, _frame: Any) -> None:
    global _running
    log.info(f"Received signal {sig}, shutting down")
    _running = False


def load_state() -> datetime | None:
    """Return the last-synced timestamp, or None on first run."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        ts = data.get("last_timestamp")
        if ts:
            return datetime.fromisoformat(ts)
    except (json.JSONDecodeError, KeyError, ValueError):
        log.warning("Corrupt state file, starting from scratch")
    return None


def save_state(ts: datetime) -> None:
    """Persist the last-synced timestamp atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_timestamp": ts.isoformat()}))
    tmp.rename(STATE_FILE)


async def _fetch_all_records_async(
    mac: str,
    entry_filter: dict[str, Any],
) -> aranet4.client.Record:
    # Uses aranet4 private API _all_records (validated against aranet4>=2.6.0).
    # Prefer a public timeout-aware API upstream if aranet4 adds one.
    async with asyncio.timeout(BLE_FETCH_TIMEOUT):
        # remove_empty slices to the requested time range. False keeps the
        # full log, with -1 placeholders for indexes that were not fetched.
        return await aranet4.client._all_records(mac, entry_filter, remove_empty=True)


def _format_fetch_error(exc: BaseException) -> str:
    if isinstance(exc, _DBUS_TRANSPORT_ERRORS):
        return f"D-Bus connection lost ({type(exc).__name__})"
    message = str(exc).strip()
    if message:
        return message
    return f"{type(exc).__name__} (no message)"


def _reset_bleak_bluez_manager() -> None:
    """Drop Bleak's cached BlueZ manager so the next scan opens a fresh D-Bus bus.

    Bleak caches one BlueZManager per event loop. After the D-Bus socket dies
    (EOFError), that instance is unusable until it is disconnected and removed.
    """
    try:
        from bleak.backends.bluezdbus.manager import _global_instances
    except ImportError:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    manager = _global_instances.pop(loop, None)
    if manager is None:
        return
    bus = getattr(manager, "_bus", None)
    if bus is None:
        return
    try:
        bus.disconnect()
    except Exception:
        log.debug("Ignoring error while closing stale BlueZ D-Bus bus")


async def fetch_records(
    mac: str,
    since: datetime | None,
) -> list[aranet4.client.RecordItem]:
    # humi=True is required so aranet4 selects HUMIDITY2 for AranetRn+.
    entry_filter: dict[str, Any] = {"temp": True, "humi": True, "pres": True}
    if since is not None:
        entry_filter["start"] = since + timedelta(seconds=1)
    suffix = f" since {since.isoformat()}" if since else " (full history)"
    log.info(f"Fetching records{suffix}")
    last_exc: BaseException | None = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        log.info(f"BLE fetch attempt {attempt}/{CONNECT_RETRIES}")
        try:
            history = await _fetch_all_records_async(mac, entry_filter)
            log.info(f"Received {len(history.value)} records ({history.records_on_device} on device)")
            return history.value
        except TimeoutError as exc:
            last_exc = exc
            detail = f"timed out after {BLE_FETCH_TIMEOUT}s"
        except Exception as exc:
            last_exc = exc
            detail = _format_fetch_error(exc)
        _reset_bleak_bluez_manager()
        if attempt == CONNECT_RETRIES:
            break
        log.warning(
            f"BLE fetch attempt {attempt}/{CONNECT_RETRIES} failed: {detail} — retrying in {CONNECT_RETRY_DELAY}s"
        )
        await asyncio.sleep(CONNECT_RETRY_DELAY)
    raise RuntimeError(f"BLE fetch failed after {CONNECT_RETRIES} attempts") from last_exc


BATCH_CHECKPOINT_SIZE = 100
NO_DATA_SENTINEL = -1


def _sensor_values(rec: aranet4.client.RecordItem) -> tuple[float | int, ...]:
    return (
        rec.temperature,
        rec.humidity,
        rec.pressure,
        rec.radon_concentration,
    )


def _is_empty_reading(rec: aranet4.client.RecordItem) -> bool:
    """Return True if every sensor field is the aranet4 no-data sentinel."""
    return all(v == NO_DATA_SENTINEL for v in _sensor_values(rec))


def _sensor_value(value: float | int) -> float | int | None:
    return None if value == NO_DATA_SENTINEL else value


def publish_records(
    client: mqtt.Client,
    records: list[aranet4.client.RecordItem],
) -> datetime | None:
    """Publish records to MQTT and return the last successfully published timestamp.

    Saves state every BATCH_CHECKPOINT_SIZE publishes so that a crash
    mid-batch only requires re-sending the tail, not the full batch.
    Empty placeholder records do not advance the sync cursor. A missing
    individual field is published as null and still advances the cursor.
    """
    topic = f"{MQTT_TOPIC_PREFIX}/{DEVICE_NAME}/measurement"
    last_published: datetime | None = None
    published = 0
    for i, rec in enumerate(records, 1):
        if _is_empty_reading(rec):
            log.debug(f"Skipping empty record {rec.date}")
            continue
        payload = json.dumps(
            {
                "timestamp": rec.date.isoformat(),
                "temperature": _sensor_value(rec.temperature),
                "humidity": _sensor_value(rec.humidity),
                "pressure": _sensor_value(rec.pressure),
                "radon": _sensor_value(rec.radon_concentration),
            }
        )
        info = client.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
        if not info.is_published():
            raise TimeoutError(f"MQTT publish timed out after {PUBLISH_TIMEOUT}s")
        last_published = rec.date
        published += 1
        if published % BATCH_CHECKPOINT_SIZE == 0:
            save_state(last_published)
            log.info(f"Checkpoint at {i}/{len(records)} records")
    skipped = len(records) - published
    if published:
        log.info(f"Published {published} records")
    if skipped:
        log.info(f"Skipped {skipped} empty records")
    return last_published


def connect_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"aranet-{DEVICE_NAME}",
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS or None)
    use_tls = MQTT_TLS if MQTT_TLS is not None else (MQTT_PORT == 8883)
    if use_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.enable_logger(log)
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError as exc:
            if attempt == CONNECT_RETRIES:
                raise
            log.warning(
                f"MQTT connect attempt {attempt}/{CONNECT_RETRIES} failed: {exc} — retrying in {CONNECT_RETRY_DELAY}s"
            )
            time.sleep(CONNECT_RETRY_DELAY)
    client.loop_start()
    return client


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not ARANET_MAC:
        log.error("ARANET_MAC environment variable is required")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Starting aranet-to-mqtt bridge")
    initial = load_state()
    if initial:
        log.info(f"Resuming from {initial.isoformat()}")
    else:
        log.info("No saved cursor; fetching full device history")
    log.info(f"  Device MAC : {ARANET_MAC}")
    use_tls = MQTT_TLS if MQTT_TLS is not None else (MQTT_PORT == 8883)
    log.info(f"  MQTT broker: {MQTT_HOST}:{MQTT_PORT} (TLS: {use_tls})")
    log.info(f"  Topic      : {MQTT_TOPIC_PREFIX}/{DEVICE_NAME}/measurement")
    log.info(f"  Poll every : {POLL_INTERVAL}s")
    log.info(f"  BLE timeout: {BLE_FETCH_TIMEOUT}s")
    log.info(f"  State file : {STATE_FILE}")

    client = connect_mqtt()
    try:
        asyncio.run(_run_poll_loop(client))
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("Shutdown complete")


async def _run_poll_loop(client: mqtt.Client) -> None:
    while _running:
        last_ts = load_state()
        try:
            records = await fetch_records(ARANET_MAC, last_ts)
        except Exception:
            log.exception("Failed to fetch records from device")
            await _sleep(POLL_INTERVAL)
            continue

        if not records:
            log.info("No new records")
            await _sleep(POLL_INTERVAL)
            continue

        try:
            latest = publish_records(client, records)
        except Exception:
            log.exception("Failed to publish records to MQTT")
            await _sleep(POLL_INTERVAL)
            continue

        if latest:
            save_state(latest)
            log.info(f"Synced up to {latest.isoformat()}")

        await _sleep(POLL_INTERVAL)


async def _sleep(seconds: int) -> None:
    """Sleep in small increments so signals can interrupt promptly."""
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        await asyncio.sleep(min(1.0, end - time.monotonic()))


if __name__ == "__main__":
    main()
