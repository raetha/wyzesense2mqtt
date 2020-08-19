# see hooks/build and hooks/.config
ARG BASE_IMAGE_PREFIX
FROM ${BASE_IMAGE_PREFIX}python:3.8-slim-buster

# see hooks/post_checkout
ARG ARCH
COPY qemu-${ARCH}-static /usr/bin

# Begin WyzeSense2MQTT
LABEL maintainer="Raetha"

COPY wyzesense2mqtt /wyzesense2mqtt/

RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /wyzesense2mqtt/requirements.txt \
    && chmod u+x /wyzesense2mqtt/service.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends
       vim \
    && rm -rf /var/lib/apt/lists/*

VOLUME /wyzesense2mqtt/config /wyzesense2mqtt/logs

ENTRYPOINT /wyzesense2mqtt/service.sh
