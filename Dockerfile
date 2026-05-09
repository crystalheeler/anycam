ARG BUILD_FROM
FROM $BUILD_FROM
ARG BUILD_ARCH

# 2.6.0-rc2.0: pinned-version build with ffmpeg sourced from
# archive.raspberrypi.com so we get the v4l2-request HEVC patches that
# light up Pi 4 / Pi 5 rpivid hardware decode. Everything else stays
# from Debian Bookworm. apt-pinning enforces the split: only ffmpeg
# and its libav siblings come from rpios; every other package
# (including transitive deps) comes from Debian.
#
# === Pinned versions (the contract this Dockerfile enforces) ===
#
# Debian Bookworm packages:
#   python3=3.11.2-1+b1
#   python3-pip=23.0.1+dfsg-1+deb12u1
#   nmap=7.93+dfsg1-1
#   net-tools=2.10-0.1+deb12u2
#   iproute2=6.1.0-3
#
# Raspberry Pi OS packages (aarch64 only):
#   ffmpeg=8:5.1.3-1+rpt4
#   (libavcodec59, libavformat59, libavfilter8, libavdevice59,
#    libavutil57, libswscale6, libswresample4, libpostproc56 are
#    transitively pinned by ffmpeg's own =-pinned Depends declarations)
#
# PyPI packages:
#   aiohttp==3.13.5
#   cryptography==48.0.0
#
# If any of these versions has rotated out of its source repo by the
# time this Dockerfile is built, apt or pip will fail loudly and we
# bump to the new current version in a follow-up rc. That's the
# pinning contract: known versions, fail-loud on drift.
#
# === aarch64 / amd64 split ===
#
# aarch64 builds add archive.raspberrypi.com as a secondary apt source
# and use apt-pinning to source ffmpeg + libav* from there. amd64
# builds skip the rpios source entirely and use Debian's own ffmpeg —
# there's no rpivid hardware on x86 to drive, so the v4l2-request
# patches are irrelevant. Net effect: amd64 image stays small and
# Debian-pure; aarch64 image gains one external source pinned to
# specific package versions.

# Step 1: on aarch64, register archive.raspberrypi.com as a secondary
# apt source with its signing key trusted, plus apt-preferences pinning
# that allows ONLY ffmpeg + libav* to be installed from it. Every
# other package on the system (including any deps that happen to also
# exist in rpios) comes from Debian via Pin-Priority.
RUN if [ "${BUILD_ARCH}" = "aarch64" ]; then \
        echo "==> aarch64 build: registering archive.raspberrypi.com as pinned secondary apt source for ffmpeg" \
        && apt-get update \
        && apt-get install -y --no-install-recommends \
            ca-certificates curl gnupg \
        && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
            | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] http://archive.raspberrypi.com/debian/ bookworm main" \
            > /etc/apt/sources.list.d/raspi.list \
        && printf 'Package: *\nPin: release o=Raspberry Pi Foundation\nPin-Priority: 1\n\nPackage: ffmpeg libavcodec* libavformat* libavfilter* libavdevice* libavutil* libswscale* libswresample* libpostproc*\nPin: release o=Raspberry Pi Foundation\nPin-Priority: 990\n' \
            > /etc/apt/preferences.d/00-raspi-ffmpeg \
        && apt-get update ; \
    else \
        echo "==> ${BUILD_ARCH} build: using Debian-only sources (no rpivid hardware on this arch)" ; \
    fi

# Step 2: install the pinned set. ffmpeg-version varies by arch
# (rpios on aarch64, Debian on amd64) so we branch the install.
# All other packages are pinned to the same Debian-Bookworm versions
# regardless of arch.
#
# 2.6.0-rc2.3 — pin lock-in. Versions for ffmpeg and python3-pip
# captured from the 2.6.0-rc2.1 discovery build log on CrystalHeeler's Pi 4
# (Pi-with-rpivid system). All 8 dependency pins now strict; if any
# rotates out by build time, apt or pip fails loudly and we bump in
# a follow-up rc. No more no-version-constraint installs.
RUN if [ "${BUILD_ARCH}" = "aarch64" ]; then \
        FFMPEG_PIN="ffmpeg=8:5.1.8-0+deb12u1+rpt1" ; \
    else \
        FFMPEG_PIN="ffmpeg=7:5.1.8-0+deb12u1" ; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3=3.11.2-1+b1 \
        python3-pip=23.0.1+dfsg-1 \
        nmap=7.93+dfsg1-1 \
        net-tools=2.10-0.1+deb12u2 \
        iproute2=6.1.0-3 \
        ${FFMPEG_PIN} \
    && pip3 install --break-system-packages \
        aiohttp==3.13.5 \
        cryptography==48.0.0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY camera_discovery.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
