FROM alpine:latest

LABEL maintainer="Raetha"

ENV TZ="America/New_York"

RUN apk add --update python3 py-pip git tzdata && \
    rm -rf /var/cache/apk/*

RUN mkdir -p /opt/ha-wyzesense && \
    git clone -b "0.0.7" https://github.com/kevinvincent/ha-wyzesense /opt/ha-wyzesense

RUN mkdir -p /opt/wyzesense2mqtt && \
    git clone https://github.com/raetha/wyzesense2mqtt.git /opt/wyzesense2mqtt && \
    pip3 install -r /opt/wyzesense2mqtt/requirements.txt && \
    ln -s /opt/ha-wyzesense/custom_components/wyzesense/wyzesense_custom.py /opt/wyzesense2mqtt/ && \
    chmod u+x /opt/wyzesense2mqtt/service.sh

VOLUME /opt/wyzesense2mqtt/config

ENTRYPOINT /opt/wyzesense2mqtt/service.sh
