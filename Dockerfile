# see hooks/build and hooks/.config
ARG BASE_IMAGE_PREFIX
FROM ${BASE_IMAGE_PREFIX}alpine

# see hooks/post_checkout
ARG ARCH
COPY qemu-${ARCH}-static /usr/bin

# Begin WyzeSense2MQTT
LABEL maintainer="Raetha"

ENV TZ="America/New_York"

COPY wyzesense2mqtt /wyzesense2mqtt/
COPY wyzesense2mqtt/config /wyzesense2mqtt/config/

RUN apk add --update \
        py3-pip \
        python3 \
        tzdata \
    && rm -rf /var/cache/apk/* \
    && pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /wyzesense2mqtt/requirements.txt \
    && chmod u+x /wyzesense2mqtt/service.sh

VOLUME /wyzesense2mqtt/config /wyzesense2mqtt/logs

ENTRYPOINT /wyzesense2mqtt/service.sh
