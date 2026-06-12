# Artea policy-sync service. Stdlib-only: constraints are pushed to devpi via
# its JSON HTTP API (devpi-client cannot be used behind --outside-url, see
# devpi/README.md), so no pip dependencies at all.
# Base = python:3.12-slim, digest-pinned (ADR-0004); bump procedure in
# docs/guides/operations.md ("Base-image digest pins").
FROM python:3.14-slim@sha256:d7a925f9eb9639a93e455b9f12c167569358818c0f62b51b88edbc8fcf34c421

WORKDIR /app
COPY policy_sync ./policy_sync
COPY --chmod=755 entrypoint.sh /entrypoint.sh

# Non-root service user. A *fresh* `policy-data` named volume inherits this
# ownership from the image's /policy; volumes created by an older (root-only)
# image are repaired by the entrypoint, which starts as root, chowns /policy,
# then drops to this user before exec'ing the service.
RUN useradd --uid 8920 --create-home policysync \
 && mkdir -p /policy \
 && chown policysync:policysync /policy

ENV PYTHONUNBUFFERED=1 \
    POLICY_DIR=/policy

EXPOSE 8920
VOLUME ["/policy"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8920/healthz', timeout=4)"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "policy_sync"]
