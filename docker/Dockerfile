FROM alpine:latest

LABEL maintainer="Raetha"

ENV TZ="America/New_York"

RUN apk add --update python3 py-pip git && \
    rm -rf /var/cache/apk/* && \
    mkdir -p /opt/wyze-mqtt && \
    mkdir -p /opt/ha-wyzesense && \
    git clone https://github.com/raetha/wyze-mqtt.git /opt/wyze-mqtt && \
    pip3 install -r /opt/wyze-mqtt/requirements.txt && \
    git clone https://github.com/kevinvincent/ha-wyzesense /opt/ha-wyzesense && \
    ln -s /opt/ha-wyzesense/wyzesense_custom.py /opt/wyze-mqtt/ && \
    chmod u+x /opt/wyze-mqtt/service.sh

VOLUME /opt/wyze-mqtt/config

ENTRYPOINT /opt/wyze-mqtt/service.sh
