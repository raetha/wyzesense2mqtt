FROM alpine:latest

MAINTAINER Raetha

RUN apk add --update python3 py-pip git && \
    pip3 install -r requirements.txt && \
    rm -rf /var/cache/apk/* && \
    mkdir -p /data/wyze-mqtt && \
    git clone https://github.com/raetha/wyze-mqtt.git /data/wyze-mqtt && \
    chmod u+x /data/wyze-mqtt/service.sh

VOLUME /docker/wyze-mqtt/config.json:/data/wyze-mqtt/config.json

ENTRYPOINT /data/wyze-mqtt/service.sh
