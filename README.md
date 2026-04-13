# aranet-to-mqtt

Periodically reads historical data (radon, temperature, humidity, pressure) from
an **Aranet RN+** sensor over Bluetooth and publishes it to an MQTT broker.

Sync progress is persisted to a state file so the container can be restarted
without re-sending old measurements.

## Prerequisites

- **Aranet RN+** with "Smart Home integrations" enabled in the Aranet Home app.
- A host with a **Bluetooth adapter** (the k3s node running the pod).
- An **MQTT broker** (e.g. Mosquitto) reachable from the container.

## Build

```bash
docker build -t aranet-to-mqtt .
```

## Run

```bash
docker run -d \
  --name aranet-to-mqtt \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  -v aranet-state:/data \
  -e ARANET_MAC=AA:BB:CC:DD:EE:FF \
  -e MQTT_HOST=mosquitto \
  aranet-to-mqtt
```

`--net=host` and `--privileged` are required for Bluetooth access.
The `/data` volume persists sync state across restarts.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ARANET_MAC` | *(required)* | Bluetooth MAC address of the Aranet RN+ |
| `MQTT_HOST` | `mosquitto` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | | MQTT username (optional) |
| `MQTT_PASS` | | MQTT password (optional, requires `MQTT_USER`) |
| `MQTT_TOPIC_PREFIX` | `aranet` | Prefix for MQTT topics |
| `DEVICE_NAME` | `rn_plus` | Device name used in the topic path |
| `POLL_INTERVAL` | `300` | Seconds between fetch cycles |
| `STATE_FILE` | `/data/state.json` | Path to the sync state file |
| `PUBLISH_TIMEOUT` | `30` | Per-message MQTT publish timeout in seconds |

## MQTT topic

Each measurement is published to:

```
{MQTT_TOPIC_PREFIX}/{DEVICE_NAME}/measurement
```

Default: `aranet/rn_plus/measurement`

Payload example:

```json
{
  "timestamp": "2026-04-13T14:30:00",
  "temperature": 21.5,
  "humidity": 45,
  "pressure": 1013.2,
  "radon": 120
}
```

## k3s deployment

The pod needs host networking, privileged access, and D-Bus for BlueZ.
Apply the PVC first, then the Deployment. Replace `AA:BB:CC:DD:EE:FF` with your
device MAC and adjust `MQTT_HOST` to match your Mosquitto service name.

### PersistentVolumeClaim

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: aranet-state
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 16Mi
```

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aranet-to-mqtt
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aranet-to-mqtt
  template:
    metadata:
      labels:
        app: aranet-to-mqtt
    spec:
      hostNetwork: true
      nodeSelector:
        bluetooth: "true"
      containers:
        - name: aranet-to-mqtt
          image: aranet-to-mqtt:latest
          securityContext:
            privileged: true
          env:
            - name: ARANET_MAC
              value: "AA:BB:CC:DD:EE:FF"
            - name: MQTT_HOST
              value: "mosquitto"
          volumeMounts:
            - name: dbus
              mountPath: /var/run/dbus
            - name: state
              mountPath: /data
      volumes:
        - name: dbus
          hostPath:
            path: /var/run/dbus
        - name: state
          persistentVolumeClaim:
            claimName: aranet-state
```

Label the node that has a Bluetooth adapter so the pod gets scheduled there:

```bash
kubectl label node <node-name> bluetooth=true
```

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
```
