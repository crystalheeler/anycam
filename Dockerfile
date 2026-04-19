ARG BUILD_FROM
FROM $BUILD_FROM

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    nmap ffmpeg \
    net-tools iproute2 \
    && pip3 install --break-system-packages \
    aiohttp cryptography \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY camera_discovery.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
