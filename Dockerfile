FROM docker.io/python:alpine

LABEL maintainer="Raetha"

COPY wyzesense2mqtt /app/

# Install project dependencies and set permissions
RUN apk add --no-cache tzdata
    && pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && chmod +x /app/service.sh

VOLUME /app/config /app/logs

ENTRYPOINT ["/app/service.sh"]
