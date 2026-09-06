
##### Optional DLSS5 node support (Blackwell image only)
# No custom node, neural runtime, Proton archive or driver DLLs are redistributed.
# Pin the Wine version that passed the RTX PRO 6000 / driver 595.84 GPU tests.
USER root
ARG DLSS5_WINE_VERSION=11.17~noble-1
RUN dpkg --add-architecture i386 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key -o /etc/apt/keyrings/winehq-archive.key \
    && echo 'd965d646defe94b3dfba6d5b4406900ac6c81065428bf9d9303ad7a72ee8d1b8  /etc/apt/keyrings/winehq-archive.key' | sha256sum -c - \
    && printf '%s\n' 'deb [arch=amd64,i386 signed-by=/etc/apt/keyrings/winehq-archive.key] https://dl.winehq.org/wine-builds/ubuntu noble main' > /etc/apt/sources.list.d/winehq.list \
    && apt-get update \
    && apt-get install -y --install-recommends "winehq-devel=${DLSS5_WINE_VERSION}" \
    && apt-get install -y --no-install-recommends \
        g++-mingw-w64-x86-64 vulkan-tools xauth x11-xserver-utils \
        xserver-xorg-core xserver-xorg-video-dummy \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=644 dlss5-support/ /opt/comfy-dlss5/
RUN chmod 0555 /opt/comfy-dlss5 /opt/comfy-dlss5/comfy-dlss5 \
    && chmod 0444 /opt/comfy-dlss5/support.py /opt/comfy-dlss5/xorg-dummy.conf \
    && ln -s /opt/comfy-dlss5/comfy-dlss5 /usr/local/bin/comfy-dlss5-setup \
    && ln -s /opt/comfy-dlss5/comfy-dlss5 /usr/local/bin/comfy-dlss5-wine \
    && wine --version \
    && x86_64-w64-mingw32-g++ --version
USER comfytoo
