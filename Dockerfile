FROM alpine:latest

LABEL maintainer="Raetha"

ENV TZ="America/New_York"

RUN apk add --update \
        py3-pip \
        python3 \
        tzdata \
    && rm -rf /var/cache/apk/*

COPY requirements.txt /wyzesense2mqtt/
RUN pip3 install --upgrade pip \
    && pip3 install -r /wyzesense2mqtt/requirements.txt

COPY . /wyzesense2mqtt/
RUN  chmod u+x /wyzesense2mqtt/service.sh

VOLUME /wyzesense2mqtt/config /wyzesense2mqtt/logs

ENTRYPOINT /wyzesense2mqtt/service.sh