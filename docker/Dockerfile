FROM alpine:latest

LABEL maintainer="Raetha"

ENV TZ="America/New_York"

RUN apk add --update python3 py-pip git && \
    rm -rf /var/cache/apk/* && \
    mkdir -p /opt/wyzesense2mqtt && \
    mkdir -p /opt/ha-wyzesense && \
    git clone https://github.com/raetha/wyzesense2mqtt.git /opt/wyzesense2mqtt && \
    pip3 install -r /opt/wyzesense2mqtt/requirements.txt && \
    git clone -b "0.0.7" https://github.com/kevinvincent/ha-wyzesense /opt/ha-wyzesense && \
    ln -s /opt/ha-wyzesense/custom_components/wyzesense/wyzesense_custom.py /opt/wyzesense2mqtt/ && \
    chmod u+x /opt/wyzesense2mqtt/service.sh

VOLUME /opt/wyzesense2mqtt/config

ENTRYPOINT /opt/wyzesense2mqtt/service.sh
