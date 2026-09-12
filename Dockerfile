# Python 3.14 on Debian 13 (trixie), whose ffmpeg 7.x is built with libx265.
# There are no Python dependencies: config is read with the stdlib tomllib and
# the web panel is served by http.server, so there is nothing to pip install
# and nothing to drift.
FROM python:3.14-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRANSCODER_CONFIG=/config/config.toml

WORKDIR /opt/transcoder
COPY app/ ./app/

RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin transcoder \
 && mkdir -p /config /temp /media \
 && chown -R transcoder:transcoder /config /temp /opt/transcoder

USER transcoder
EXPOSE 8080

# tini reaps ffmpeg children and forwards SIGTERM, so `docker stop` cancels
# running encodes cleanly instead of orphaning them.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "app"]
CMD ["daemon"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=4)"]
