ARG BUILD_FROM
FROM $BUILD_FROM
ARG BUILD_ARCH

# 2.6.0-rc1.0: bundle a custom-compiled rpi-ffmpeg on aarch64 builds so
# Pi 4 / Pi 5 users get HEVC hardware decode via rpivid. Previously the
# system Debian Bookworm ffmpeg only had the stateful v4l2 m2m API,
# which on Pi 4 means HEVC hits CPU (bcm2835-codec exposes H264/MPEG/
# VP8/VP9/VC1 via m2m but NOT HEVC). The HEVC silicon is in rpivid,
# which speaks the v4l2-request stateless API exclusively. ffmpeg
# needs --enable-v4l2-request to talk to it, which mainline Debian
# Bookworm does not provide. So we compile rpi-ffmpeg from source on
# aarch64 builds only. amd64 builds skip the entire compile and rely
# on system /usr/bin/ffmpeg as before — no rpivid hardware on x86 to
# light up.
#
# rpi-ffmpeg is jc-kynesim/rpi-ffmpeg release/6.1: a fork of ffmpeg
# 6.1 with the v4l2-request HEVC patches that Jernej Skrabec et al
# upstream-merge incrementally. This is also what Raspberry Pi OS
# ships as its `ffmpeg` package.
#
# Runtime impact: aarch64 image carries an extra ~50MB plus the build
# toolchain. We don't apt-get purge after compile because (a) it makes
# this Dockerfile much harder to reason about with --auto-remove
# unpredictably yanking shared libs, and (b) image size on a one-time-
# pulled HA addon is not a meaningful constraint. amd64 image stays
# at the previous size.
#
# PATH wiring: run.sh prepends /opt/rpi-ffmpeg/bin to PATH so plain
# `ffmpeg` resolves to the bundled binary on aarch64. On amd64 the
# directory doesn't exist (compile skipped) and PATH lookup falls
# through to /usr/bin/ffmpeg. So no Python-side branching needed —
# the same `ffmpeg` invocation does the right thing on each arch.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    nmap ffmpeg \
    net-tools iproute2 \
    && pip3 install --break-system-packages \
    aiohttp cryptography \
    && if [ "${BUILD_ARCH}" = "aarch64" ]; then \
        echo "==> aarch64 build: compiling rpi-ffmpeg with --enable-v4l2-request" \
        && apt-get install -y --no-install-recommends \
            build-essential pkg-config \
            libdrm-dev libudev-dev \
            nasm yasm \
            git ca-certificates \
        && git clone --depth=1 -b release/6.1 \
            https://github.com/jc-kynesim/rpi-ffmpeg.git /tmp/rpi-ffmpeg \
        && cd /tmp/rpi-ffmpeg \
        && ./configure \
            --prefix=/opt/rpi-ffmpeg \
            --enable-v4l2-request \
            --enable-libdrm \
            --enable-libudev \
            --disable-debug --disable-doc \
            --disable-htmlpages --disable-manpages \
            --disable-podpages --disable-txtpages \
            --disable-mmal \
        && make -j$(nproc) \
        && make install \
        && cd / && rm -rf /tmp/rpi-ffmpeg ; \
    else \
        echo "==> ${BUILD_ARCH} build: skipping rpi-ffmpeg compile (rpivid is Pi-only)" ; \
    fi \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY camera_discovery.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
