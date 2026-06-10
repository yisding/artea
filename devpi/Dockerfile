# Artea devpi image: PyPI pull-through cache + devpi-constrained filtering.
# Internal-only service; the gateway enforces auth (see docs/ARCHITECTURE.md).
# Base = python:3.12-slim, digest-pinned (ADR-0004); bump procedure in
# docs/guides/operations.md ("Base-image digest pins").
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

# Exact pins; docker compose overrides these from .env so all pins live there.
ARG DEVPI_SERVER_VERSION=6.20.1
ARG DEVPI_CLIENT_VERSION=7.3.0
ARG DEVPI_CONSTRAINED_VERSION=2.1.0

RUN pip install --no-cache-dir \
      "devpi-server==${DEVPI_SERVER_VERSION}" \
      "devpi-client==${DEVPI_CLIENT_VERSION}" \
      "devpi-constrained==${DEVPI_CONSTRAINED_VERSION}"

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
