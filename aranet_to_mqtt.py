#!/usr/bin/env python3
"""Aranet RN+ to MQTT bridge.

Periodically fetches historical records from an Aranet RN+ sensor over
Bluetooth and publishes them to an MQTT broker.  Persists sync progress
to a JSON state file so that restarts do not re-send old data.
"""

from __future__ import annotations

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


def fetch_records(mac: str, since: datetime | None) -> list[aranet4.client.RecordItem]:
    entry_filter: dict[str, Any] = {}
    if since is not None:
        entry_filter["start"] = since + timedelta(seconds=1)
    suffix = f" since {since.isoformat()}" if since else " (full history)"
    log.info(f"Fetching records{suffix}")
    history = aranet4.client.get_all_records(mac, entry_filter=entry_filter)
    log.info(f"Received {len(history.value)} records ({history.records_on_device} on device)")
    return history.value


BATCH_CHECKPOINT_SIZE = 100


def publish_records(
    client: mqtt.Client,
    records: list[aranet4.client.RecordItem],
) -> datetime | None:
    """Publish records to MQTT and return the latest timestamp.

    Saves state every BATCH_CHECKPOINT_SIZE records so that a crash
    mid-batch only requires re-sending the tail, not the full batch.
    """
    topic = f"{MQTT_TOPIC_PREFIX}/{DEVICE_NAME}/measurement"
    latest: datetime | None = None
    for i, rec in enumerate(records, 1):
        payload = json.dumps(
            {
                "timestamp": rec.date.isoformat(),
                "temperature": rec.temperature,
                "humidity": rec.humidity,
                "pressure": rec.pressure,
                "radon": rec.co2,
            }
        )
        info = client.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
        if not info.is_published():
            raise TimeoutError(f"MQTT publish timed out after {PUBLISH_TIMEOUT}s")
        latest = rec.date
        if i % BATCH_CHECKPOINT_SIZE == 0:
            save_state(latest)
            log.info("Checkpoint at %d/%d records", i, len(records))
    return latest


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
                f"MQTT connect attempt {attempt}/{CONNECT_RETRIES} failed: {exc}"
                f" — retrying in {CONNECT_RETRY_DELAY}s"
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
    log.info(f"  Device MAC : {ARANET_MAC}")
    use_tls = MQTT_TLS if MQTT_TLS is not None else (MQTT_PORT == 8883)
    log.info(f"  MQTT broker: {MQTT_HOST}:{MQTT_PORT} (TLS: {use_tls})")
    log.info(f"  Topic      : {MQTT_TOPIC_PREFIX}/{DEVICE_NAME}/measurement")
    log.info(f"  Poll every : {POLL_INTERVAL}s")
    log.info(f"  State file : {STATE_FILE}")

    client = connect_mqtt()

    try:
        while _running:
            last_ts = load_state()
            try:
                records = fetch_records(ARANET_MAC, last_ts)
            except Exception:
                log.exception("Failed to fetch records from device")
                _sleep(POLL_INTERVAL)
                continue

            if not records:
                log.info("No new records")
                _sleep(POLL_INTERVAL)
                continue

            try:
                latest = publish_records(client, records)
            except Exception:
                log.exception("Failed to publish records to MQTT")
                _sleep(POLL_INTERVAL)
                continue

            if latest:
                save_state(latest)
                log.info(f"Synced up to {latest.isoformat()}")

            _sleep(POLL_INTERVAL)
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("Shutdown complete")


def _sleep(seconds: int) -> None:
    """Sleep in small increments so signals can interrupt promptly."""
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


if __name__ == "__main__":
    main()
