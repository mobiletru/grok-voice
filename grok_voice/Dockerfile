# Pinned official Home Assistant base (Alpine 3.24, amd64 + aarch64 manifests verified).
FROM ghcr.io/home-assistant/base:3.24-2026.08.0

# python3 + aiohttp from Alpine packages (no pip, no build toolchain)
RUN apk add --no-cache python3 py3-aiohttp

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ARG BUILD_VERSION
LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64"

CMD [ "/run.sh" ]
