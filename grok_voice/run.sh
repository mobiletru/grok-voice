#!/usr/bin/with-contenv bashio

bashio::log.info "Starting Grok Voice ${BUILD_VERSION:-}"

if ! bashio::config.has_value 'xai_api_key'; then
  bashio::log.warning "xai_api_key is not set - the page will load but voice will not work until you add a key in the app Configuration."
fi

if bashio::config.true 'allow_ha_control'; then
  bashio::log.warning "allow_ha_control is not implemented yet in this version; ignoring."
fi

exec python3 -u /app/server.py
