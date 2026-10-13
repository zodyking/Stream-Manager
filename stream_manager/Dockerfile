FROM ghcr.io/home-assistant/base-python:3.12-alpine3.24

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=1.0.0
ARG GO2RTC_VERSION=1.9.14

RUN apk add --no-cache ffmpeg curl iproute2 nftables font-dejavu

RUN case "${BUILD_ARCH}" in \
      aarch64) ASSET=go2rtc_linux_arm64 ;; \
      amd64) ASSET=go2rtc_linux_amd64 ;; \
      armv7) ASSET=go2rtc_linux_arm ;; \
      armhf) ASSET=go2rtc_linux_armv6 ;; \
      i386) ASSET=go2rtc_linux_i386 ;; \
      *) ASSET=go2rtc_linux_amd64 ;; \
    esac \
 && curl -fsSL -o /usr/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/${ASSET}" \
 && chmod 0755 /usr/bin/go2rtc

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY onvif_proxy /app/onvif_proxy
COPY static /app/static
COPY run.sh /app/run.sh
COPY rootfs /

RUN chmod 0755 /app/run.sh /etc/services.d/onvif-proxy/run /etc/services.d/onvif-proxy/finish

ENV PYTHONUNBUFFERED=1 \
    GO2RTC_BIN=/usr/bin/go2rtc

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="armhf|armv7|aarch64|amd64|i386"
