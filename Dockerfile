FROM docker.io/python:3.12-alpine

LABEL maintainer="Raetha"

COPY wyzesense2mqtt /app/

# Install project dependencies and set permissions
RUN apk add --no-cache tzdata jq \
    && pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && chmod +x /app/service.sh

VOLUME /app/data

# Container flips unhealthy if the bridge hangs or a dongle fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD test -f /tmp/ws2m_healthy

ENTRYPOINT ["/app/service.sh"]
