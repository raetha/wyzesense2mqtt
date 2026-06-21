FROM docker.io/python:3.12-alpine

LABEL maintainer="Raetha"

COPY wyzesense2mqtt /app/

# Install project dependencies and set permissions
RUN apk add --no-cache tzdata jq \
    && pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && chmod +x /app/service.sh

VOLUME /app/data

ENTRYPOINT ["/app/service.sh"]
