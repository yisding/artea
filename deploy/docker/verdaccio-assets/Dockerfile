# Artea verdaccio plugin assets image (ghcr.io/yisding/artea-verdaccio-assets).
# Carries the locally-built Verdaccio plugins for Kubernetes: an init container
# in the verdaccio pod copies /plugins into an emptyDir mounted at
# /verdaccio/plugins (deploy/helm/artea values.yaml) — the K8s equivalent of
# compose's bind mount. The stock verdaccio image stays untouched (R7).
#
# CI builds the plugins first, then this image with the workspace as context:
#   pnpm -C verdaccio/plugins install --frozen-lockfile
#   pnpm -C verdaccio/plugins build
#   docker build -f deploy/docker/verdaccio-assets/Dockerfile verdaccio/plugins
#
# The WHOLE workspace directory is copied, never single plugins: pnpm links
# dependencies via relative symlinks into plugins/node_modules/.pnpm
# (verdaccio/README.md), and COPY preserves those symlinks.
# busybox (not scratch) so the init container has `sh`/`cp`/`chmod`.
FROM busybox:1.38@sha256:fd8d9aa63ba2f0982b5304e1ee8d3b90a210bc1ffb5314d980eb6962f1a9715d

COPY . /plugins/
