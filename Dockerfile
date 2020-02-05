FROM alpine:latest

MAINTAINER Raetha

RUN apk add --update python3 py-pip git && \
    pip3 install -r requirements.txt && \
    rm -rf /var/cache/apk/* && \
    mkdir -p /opt/wyze-mqtt && \
    mkdir -p /opt/ha-wyzesense && \
    git clone https://github.com/raetha/wyze-mqtt.git /opt/wyze-mqtt && \
    git clone https://github.com/kevinvincent/ha-wyzesense /opt/ha-wyzesense && \
    ln -s /opt/ha-wyzesense/wyzesense_custom.py /opt/wyze-mqtt/bin/ && \
    chmod u+x /opt/wyze-mqtt/service.sh

VOLUME /docker/wyze-mqtt/config:/opt/wyze-mqtt/config

ENTRYPOINT /opt/wyze-mqtt/bin/service.sh
