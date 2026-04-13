FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez bluetooth dbus libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY aranet_to_mqtt.py .

VOLUME /data

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-u", "aranet_to_mqtt.py"]
