#!/usr/bin/with-contenv bashio

export INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PORT=8099

bashio::log.info "Camera Discovery starting on port ${INGRESS_PORT}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"

exec python3 /camera_discovery.py
