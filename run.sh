#!/usr/bin/with-contenv bashio

export INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PORT=8099

bashio::log.info "Camera Discovery starting on port ${INGRESS_PORT}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"

# Write go2rtc config
cat > /tmp/go2rtc.yaml << 'EOF'
api:
  listen: "127.0.0.1:1984"
log:
  level: warn
  format: text
EOF

# Start go2rtc in background
go2rtc -config /tmp/go2rtc.yaml &
GO2RTC_PID=$!

# Wait for go2rtc API to be ready (up to 5 seconds)
for i in $(seq 1 10); do
    if wget -qO- http://127.0.0.1:1984/api/streams > /dev/null 2>&1; then
        bashio::log.info "go2rtc ready"
        break
    fi
    sleep 0.5
done

exec python3 /camera_discovery.py
