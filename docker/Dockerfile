FROM alpine:latest

LABEL maintainer="Raetha"

ENV TZ="America/New_York"

RUN apk add --update python3 py-pip git tzdata && \
    rm -rf /var/cache/apk/*

RUN mkdir -p /opt/wyzesense2mqtt && \
    git clone https://github.com/raetha/wyzesense2mqtt.git /opt/wyzesense2mqtt && \
    pip3 install -r /opt/wyzesense2mqtt/requirements.txt && \
    chmod u+x /opt/wyzesense2mqtt/service.sh

VOLUME /opt/wyzesense2mqtt/config
VOLUME /opt/wyzesense2mqtt/logs

ENTRYPOINT /opt/wyzesense2mqtt/service.sh
