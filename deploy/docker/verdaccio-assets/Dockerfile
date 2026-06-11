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
FROM busybox:1.37@sha256:9532d8c39891ca2ecde4d30d7710e01fb739c87a8b9299685c63704296b16028

COPY . /plugins/
