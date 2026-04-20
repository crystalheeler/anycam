ARG BUILD_FROM
FROM $BUILD_FROM

ARG BUILD_ARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    nmap ffmpeg wget \
    net-tools iproute2 \
    && pip3 install --break-system-packages \
    aiohttp cryptography \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install go2rtc — handles RTSP/ONVIF/HLS/RTMP + all codecs natively
# Used by Frigate and HA's own camera integration
RUN case "${BUILD_ARCH}" in \
        aarch64) GO2RTC_ARCH="arm64"  ;; \
        amd64)   GO2RTC_ARCH="amd64"  ;; \
        armhf)   GO2RTC_ARCH="armv6"  ;; \
        armv7)   GO2RTC_ARCH="armv6"  ;; \
        i386)    GO2RTC_ARCH="386"    ;; \
        *)       GO2RTC_ARCH="amd64"  ;; \
    esac && \
    wget -qO /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.9/go2rtc_linux_${GO2RTC_ARCH}" && \
    chmod +x /usr/local/bin/go2rtc

COPY run.sh /
COPY camera_discovery.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
