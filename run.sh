#!/usr/bin/with-contenv bashio

# Ingress wiring (was the only thing here before 2.4.0-rc3.1).
export INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PORT=8099

# 2.4.0-rc3.1: read every user-configurable option from /data/options.json
# (where the HA Supervisor writes the values from the addon's Configuration
# tab) and export each as the environment variable that camera_discovery.py
# expects. Without these exports, camera_discovery.py's os.environ.get(...)
# calls all fall through to the hardcoded defaults, regardless of what the
# user toggled in the UI. This bug was present since the first config option
# was introduced — every toggle has been a no-op since then. See changelog
# entry for full forensics.
#
# Option name mapping: config.yaml uses lowercase_with_underscores (the HA
# convention); camera_discovery.py reads UPPERCASE_WITH_UNDERSCORES env vars.
# bashio::config returns "true"/"false" for bool options, which is what the
# Python's `.lower() == "true"` check expects.

# Boolean toggles
export LOW_FPS_MODE=$(bashio::config 'low_fps_mode')
export SKIP_NONREF=$(bashio::config 'skip_nonref')
export LIMIT_THREADS=$(bashio::config 'limit_threads')
export STAGGER_POLLING=$(bashio::config 'stagger_polling')
export HW_DECODE=$(bashio::config 'hw_decode')
export ADAPTIVE_QUALITY=$(bashio::config 'adaptive_quality')
export FAST_STREAM_START=$(bashio::config 'fast_stream_start')
export LOW_LATENCY=$(bashio::config 'low_latency')
export UNRESTRICTED_STORAGE_BROWSER=$(bashio::config 'unrestricted_storage_browser')

# Log-level toggles
export LOG_DEBUG=$(bashio::config 'log_debug')
export LOG_INFO=$(bashio::config 'log_info')
export LOG_WARNING=$(bashio::config 'log_warning')
export LOG_ERROR=$(bashio::config 'log_error')

# String / int options
export RECORDINGS_PATH=$(bashio::config 'recordings_path')
export MOTION_SENSITIVITY=$(bashio::config 'motion_sensitivity')
export MOTION_COOLDOWN_SECS=$(bashio::config 'motion_cooldown_secs')
export MOTION_CLIP_PADDING_SECS=$(bashio::config 'motion_clip_padding_secs')

bashio::log.info "Camera Discovery starting on port ${INGRESS_PORT}"
bashio::log.info "Ingress path: ${INGRESS_PATH}"
# 2.4.0-rc3.1: log the config values once so future debugging can confirm
# at a glance whether the env-var pipeline is intact. If a toggle's value
# in the addon Configuration tab differs from what shows up here, the
# pipeline is broken — investigate run.sh first.
bashio::log.info "Config: hw_decode=${HW_DECODE} low_fps_mode=${LOW_FPS_MODE} skip_nonref=${SKIP_NONREF} limit_threads=${LIMIT_THREADS} stagger_polling=${STAGGER_POLLING} adaptive_quality=${ADAPTIVE_QUALITY}"
bashio::log.info "Config: fast_stream_start=${FAST_STREAM_START} low_latency=${LOW_LATENCY}"
bashio::log.info "Config: recordings_path=${RECORDINGS_PATH} motion_sensitivity=${MOTION_SENSITIVITY} motion_cooldown=${MOTION_COOLDOWN_SECS}s motion_padding=${MOTION_CLIP_PADDING_SECS}s"
bashio::log.info "Config: log_debug=${LOG_DEBUG} log_info=${LOG_INFO} log_warning=${LOG_WARNING} log_error=${LOG_ERROR} unrestricted_storage_browser=${UNRESTRICTED_STORAGE_BROWSER}"

# 2.6.3: log the installed ffmpeg version at every start.
#
# Since 2.6.2 the Dockerfile does not pin ffmpeg to an exact version (see the
# comment above its install step), so the version floats inside the bookworm
# suite. The Dockerfile echoes the resolved version at build time, but the
# Supervisor shows build output only when a build FAILS. After a successful
# install, this line is the only place the version is visible.
#
# On aarch64 the version must carry a +rpt suffix: that marks the Raspberry
# Pi Foundation build, which has the patches rpivid hardware decode needs.
# Without it, apt resolved to Debian's ffmpeg, meaning the Dockerfile origin
# pin failed. Everything still appears to work while hw_decode is off, which
# is why this is checked here instead of waiting for someone to notice.
FFMPEG_VER=$(dpkg-query -W -f='${Version}' ffmpeg 2>/dev/null || echo "unknown")
bashio::log.info "ffmpeg: ${FFMPEG_VER}"
if [ "$(uname -m)" = "aarch64" ] && [[ "${FFMPEG_VER}" != *+rpt* ]]; then
    bashio::log.warning "ffmpeg ${FFMPEG_VER} is not the Raspberry Pi build (no +rpt suffix). The Dockerfile origin pin did not hold, and rpivid hardware decode will not engage."
fi

exec python3 /camera_discovery.py
