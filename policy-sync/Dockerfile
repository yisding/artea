# Artea policy-sync service. Stdlib-only: constraints are pushed to devpi via
# its JSON HTTP API (devpi-client cannot be used behind --outside-url, see
# devpi/README.md), so no pip dependencies at all.
FROM python:3.12-slim

WORKDIR /app
COPY policy_sync ./policy_sync

# Runs as root: the shared `policy-data` named volume at /policy is created
# root-owned by docker; v1 keeps this simple (internal-only network).
ENV PYTHONUNBUFFERED=1 \
    POLICY_DIR=/policy

EXPOSE 8920
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8920/healthz', timeout=4)"]

CMD ["python", "-m", "policy_sync"]
