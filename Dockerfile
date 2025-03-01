FROM docker.io/python:alpine

LABEL maintainer="Raetha"

COPY wyzesense2mqtt /app/

RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && chmod +x /app/service.sh

VOLUME /app/config /app/logs

ENTRYPOINT ["/app/service.sh"]
