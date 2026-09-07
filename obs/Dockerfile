# OBS Studio built from source. Headless: KasmVNC virtual display + web
# client for browser access, obs-websocket (in-tree since OBS 28) for control.
#
# Source build rather than a package because of ARM64: Oracle Ampere A1 has no
# official OBS Linux ARM64 package. On amd64 the obsproject PPA would be
# faster, but the same source build works there too and keeps one Dockerfile
# for both - see the TARGETARCH notes further down.
#
# Browser sources (CEF) are disabled - CEF is the one dependency that is
# genuinely painful on Linux ARM64. Everything else is stock.

FROM ubuntu:24.04 AS build
ARG OBS_VERSION=31.0.3
# Set automatically by BuildKit. Used here to keep QSV - an x86-only encoder
# whose build and runtime packages are all amd64-only - out of the arm64
# build entirely, so that one stays exactly what it was.
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build pkg-config git ca-certificates curl \
        # FFmpeg (Ubuntu 24.04 builds ship libsrt support -> Media Source can
        # open srt:// URLs directly)
        libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev \
        libavutil-dev libswresample-dev libswscale-dev \
        # video/encode
        libx264-dev libv4l-dev libdrm-dev libva-dev \
        # core libs
        libcurl4-openssl-dev libmbedtls-dev libjansson-dev libpci-dev \
        libspeexdsp-dev libudev-dev uthash-dev libglib2.0-dev \
        # audio
        libpulse-dev libasound2-dev \
        # display / GL
        libgl1-mesa-dev libgles2-mesa-dev libglvnd-dev libegl1-mesa-dev \
        libx11-dev libx11-xcb-dev libxcb1-dev libxcb-randr0-dev \
        libxcb-shm0-dev libxcb-xinerama0-dev libxcb-composite0-dev \
        libxcb-xfixes0-dev libxcb-xinput-dev libxcomposite-dev libxinerama-dev libxss-dev \
        # Qt6 UI
        qt6-base-dev qt6-base-private-dev qt6-svg-dev \
        # obs-websocket deps
        libasio-dev libwebsocketpp-dev nlohmann-json3-dev libqrcodegencpp-dev \
        # fonts/text
        libfontconfig-dev libfreetype6-dev \
        # rist/srt output protocols
        librist-dev libsrt-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

# amd64-only build dependencies, separate from the big install above because
# none of these packages exist on arm64 (see the ARG TARGETARCH comment).
#
#   libvpl-dev   oneVPL headers for obs-qsv11
#   everything else: CEF's link-time dependencies. libcef.so is a full
#   Chromium and declares ~30 DT_NEEDED entries; the ones already covered by
#   the main install above are omitted here. Missing any of them fails at
#   LINK time, not configure time, with "libcef.so: undefined reference to
#   ..." while building obs-browser-page - the runtime libs in the final
#   stage do not help, the linker needs the -dev packages here.
#
#   To re-derive this list after a CEF bump:
#     readelf -d cef/Release/libcef.so | grep NEEDED
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libvpl-dev \
            libnss3-dev libnspr4-dev libdbus-1-dev libcups2-dev \
            libatk1.0-dev libatk-bridge2.0-dev libatspi2.0-dev \
            libxdamage-dev libxext-dev libxfixes-dev libxrandr-dev \
            libgbm-dev libexpat1-dev libxkbcommon-dev \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN git clone --branch ${OBS_VERSION} --depth 1 --recurse-submodules \
        https://github.com/obsproject/obs-studio.git /src

# Chromium Embedded Framework - what browser sources are built on, and the
# only way to use a hosted alert overlay (StreamElements, Streamlabs, ...)
# inside OBS. OBS does not fetch CEF itself on Linux, so it is pinned here.
# Version, URL shape and checksum all come from OBS ${OBS_VERSION}'s own
# buildspec.json, so this is exactly what that release was tested against;
# bump all three together when OBS_VERSION moves.
#
# amd64 only, like the QSV plumbing above. buildspec does publish an
# ubuntu-aarch64 CEF, so arm64 is not impossible - it has simply never been
# exercised here, and a 300 MB untested dependency is not what the Ampere
# build should inherit silently. It keeps the browserless build it always had.
ARG CEF_VERSION=6533
ARG CEF_REVISION=3
ARG CEF_SHA256=cb7225c7a937ac4cdc9c41700061f45cccc640d696902357782e57f8250bf43a
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        curl -fsSL -o /tmp/cef.tar.xz \
            "https://cdn-fastly.obsproject.com/downloads/cef_binary_${CEF_VERSION}_linux_x86_64_v${CEF_REVISION}.tar.xz" \
        && echo "${CEF_SHA256}  /tmp/cef.tar.xz" | sha256sum -c - \
        && mkdir -p /cef \
        && tar -xJf /tmp/cef.tar.xz -C /cef --strip-components=1 \
        && rm /tmp/cef.tar.xz; \
    fi

# QSV stays OFF. It was tried on this box (i7-7700 + this container) and is
# not usable: OBS renders on llvmpipe, so frames reach the encoder in system
# memory, QSV logs ">>> app not on intel GPU", falls back to the legacy MSDK
# path (API 1.35) and rejects the config with MFX_ERR_UNSUPPORTED. Forcing
# bframes=0 to satisfy the LP/VDEnc entrypoint made it worse - OBS then
# segfaulted on startup in a loop. VAAPI is the working hardware path here;
# it takes the same system-memory frames without complaint.
#
# The plumbing below (libvpl-dev, libvpl2/libmfx1, TARGETARCH) is kept on
# purpose, so revisiting this is a one-line flip rather than the whole hunt
# again. Flip to ON only together with a fix for the llvmpipe/GL-interop
# problem, which is what actually blocks it.
# Browser follows CEF: on where the download above ran, off otherwise. Keyed
# off the extracted directory rather than TARGETARCH so the two can never
# disagree.
RUN if [ -d /cef ]; then BROWSER=ON; else BROWSER=OFF; fi; \
    cmake -S /src -B /build -G Ninja \
        -DCEF_ROOT_DIR=/cef \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_COMPILE_WARNING_AS_ERROR=OFF \
        -DENABLE_BROWSER=$BROWSER \
        -DENABLE_WEBSOCKET=ON \
        -DENABLE_SCRIPTING=OFF \
        -DENABLE_PIPEWIRE=OFF \
        -DENABLE_WAYLAND=OFF \
        -DENABLE_AJA=OFF \
        -DENABLE_DECKLINK=OFF \
        -DENABLE_VLC=OFF \
        -DENABLE_VST=OFF \
        -DENABLE_JACK=OFF \
        -DENABLE_LIBFDK=OFF \
        -DENABLE_WEBRTC=OFF \
        -DENABLE_NVENC=OFF \
        -DENABLE_QSV11=OFF \
    && cmake --build /build \
    && cmake --install /build

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        # runtime libs (qt6-base-dev pulls the full Qt runtime incl. the xcb
        # platform plugin -- heavier than strictly needed, but reliable)
        ffmpeg qt6-base-dev qt6-svg-dev libqrcodegencpp1 \
        libx264-164 libmbedtls14t64 libjansson4 libcurl4 libpci3 \
        libspeexdsp1 libudev1 libglib2.0-0 libpulse0 libasound2t64 \
        libgl1 libegl1 libgl1-mesa-dri libglx-mesa0 \
        librist4 libsrt1.5-openssl libv4l-0 \
        libva2 libva-drm2 libva-x11-2 libxcb-xinput0 \
        # linux-capture.so's xcb dependencies. Without them it fails to
        # dlopen ("cannot open shared object file") and every screen/window
        # capture source silently disappears - the only trace is one line in
        # the OBS log. Derived, not guessed; missing ones surface one at a
        # time otherwise:
        #   readelf -d /usr/local/lib/obs-plugins/linux-capture.so | grep NEEDED
        libxcb-xinerama0 libxcb-composite0 \
        # audio + misc
        pulseaudio fonts-dejavu-core ca-certificates gettext-base curl \
    && rm -rf /var/lib/apt/lists/*

# KasmVNC: replaces Xvfb+x11vnc+noVNC - it IS the X server, with server-side
# WebP/JPEG encoding and a modern web client. Far better browser quality.
# TARGETARCH is set automatically by BuildKit (amd64 on a normal Windows/
# Intel/AMD PC, arm64 on Oracle Ampere or Apple Silicon) - both KasmVNC and
# Sunshine publish release assets under exactly those two names, so this
# Dockerfile builds unmodified on either.
ARG TARGETARCH

# Intel VA-API drivers. x86 only - these packages don't exist for arm64, and
# Ampere has no GPU to drive anyway, so the whole step is skipped there and
# the ARM build stays byte-for-byte what it was.
#
# What this buys: with /dev/dri passed into the container (see
# docker-compose.igpu.yml) OBS can offer VAAPI as a stream encoder instead of
# software x264. Both drivers go in on purpose - iHD is the current one and
# what libva auto-selects on Gen9+ (Kaby Lake included), i965 is the older
# driver for exactly that generation and the fallback worth trying via
# LIBVA_DRIVER_NAME=i965 if iHD reports no encode entrypoints. vainfo comes
# along so "is the GPU actually usable in here" is one command, not a guess.
#
# libvpl2 + libmfx1 are what make the QSV encoder (built in above) actually
# usable. libvpl2 is only the oneVPL *dispatcher*; it needs a runtime behind
# it, and which one depends on the generation. libmfx-gen1.2 covers Gen12+
# (Tiger Lake and newer) - this box is a Kaby Lake i7-7700, Gen9.5, so the
# legacy Media SDK runtime in libmfx1 is the one that can drive it.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            intel-media-va-driver i965-va-driver vainfo \
            libvpl2 libmfx1 \
            # CEF is a full Chromium: it drags in the GTK/ATK/NSS stack even
            # headless, and dlopen failures here are silent - the browser
            # source simply never appears in OBS's source list. Same amd64
            # gate as the CEF download in the build stage.
            libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 \
            libcups2t64 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
            libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
            libxshmfence1 libatspi2.0-0t64 libxtst6 libxi6 \
            libdbus-1-3 libexpat1 libxext6 \
        && rm -rf /var/lib/apt/lists/*; \
    fi

ARG KASMVNC_VERSION=1.4.0
RUN curl -fsSL -o /tmp/kasmvnc.deb \
        https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_noble_${KASMVNC_VERSION}_${TARGETARCH}.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/kasmvnc.deb \
    && rm /tmp/kasmvnc.deb && rm -rf /var/lib/apt/lists/*

# Sunshine: Moonlight-protocol desktop streaming (high-quality remote OBS;
# noVNC stays as the zero-dependency fallback).
ARG SUNSHINE_VERSION=2026.516.143833
RUN curl -fsSL -o /tmp/sunshine.deb \
        https://github.com/LizardByte/Sunshine/releases/download/v${SUNSHINE_VERSION}/sunshine-ubuntu-24.04-${TARGETARCH}.deb \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/sunshine.deb \
    && rm /tmp/sunshine.deb && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local
RUN ldconfig

COPY config/ /opt/obs-config-seed/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/config \
    DISPLAY=:99 \
    QT_QPA_PLATFORM=xcb \
    LIBGL_ALWAYS_SOFTWARE=1

# 4455 obs-websocket (internal), 6080 noVNC (proxied by caddy),
# 47984-47990/48010 tcp + 47998-48000 udp Sunshine/Moonlight
EXPOSE 4455 6080 47989 47990

ENTRYPOINT ["/entrypoint.sh"]
