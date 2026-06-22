# Artea devpi image: PyPI pull-through cache + Artea policy filtering.
# Internal-only service; the gateway enforces auth (see docs/ARCHITECTURE.md).
# Base = python:3.14-slim, digest-pinned (ADR-0004; bump procedure in policy-sync/Dockerfile).
FROM python:3.14-slim@sha256:d7a925f9eb9639a93e455b9f12c167569358818c0f62b51b88edbc8fcf34c421

# Exact pins; the single source of truth for the devpi-server version.
ARG DEVPI_SERVER_VERSION=6.20.2

COPY artea_devpi_policy /tmp/artea_devpi_policy
RUN pip install --no-cache-dir \
      "devpi-server==${DEVPI_SERVER_VERSION}" \
      /tmp/artea_devpi_policy \
 && rm -rf /tmp/artea_devpi_policy

# non-root; a named volume mounted at /devpi/server inherits this ownership
RUN useradd --uid 1000 --create-home devpi \
 && mkdir -p /devpi/server \
 && chown -R devpi:devpi /devpi

COPY --chmod=755 entrypoint.sh /entrypoint.sh
COPY --chmod=644 ensure_index.py /ensure_index.py

ENV DEVPISERVER_SERVERDIR=/devpi/server
USER devpi
EXPOSE 3141
VOLUME ["/devpi/server"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=5 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3141/+status', timeout=2)"]

ENTRYPOINT ["/entrypoint.sh"]
