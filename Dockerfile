# Python 3.14 on Debian 13 (trixie), whose ffmpeg 7.x is built with libx265.
# There are no Python dependencies: config is read with the stdlib tomllib and
# the web panel is served by http.server, so there is nothing to pip install
# and nothing to drift.
FROM python:3.14-slim-trixie

# util-linux for setpriv, which the entrypoint uses to drop from root to
# PUID:PGID without pulling in gosu or su-exec.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini util-linux \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRANSCODER_CONFIG=/config/config.toml \
    PUID=1000 \
    PGID=1000

WORKDIR /opt/transcoder
COPY app/ ./app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin transcoder \
 && mkdir -p /config /tmp/transcoder /media \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && chown -R transcoder:transcoder /config /tmp/transcoder /opt/transcoder

# Deliberately still root here: the entrypoint aligns the transcoder user with
# PUID/PGID, chowns what it writes to, and then steps down with setpriv. It
# also handles being started unprivileged (compose `user:`) by simply running
# as whoever it is.
EXPOSE 8080

# tini reaps ffmpeg children and forwards SIGTERM, so `docker stop` cancels
# running encodes cleanly instead of orphaning them.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["daemon"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
