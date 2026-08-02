##### Base

# uv is baked into the image instead of downloading an unpinned installer at
# every container start. It also supplies an isolated, current CPython for
# targets that set COMFY_PYTHON_VERSION.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_BIN_DIR=/usr/local/bin \
    UV_COMPILE_BYTECODE=1

RUN if [ -n "${COMFY_PYTHON_VERSION:-}" ]; then \
      uv python install --no-cache --default --install-dir "${UV_PYTHON_INSTALL_DIR}" "${COMFY_PYTHON_VERSION}"; \
      test "$(python3 -c 'import platform; print(platform.python_version())')" = "${COMFY_PYTHON_VERSION}"; \
    fi

ENV UV_PYTHON_DOWNLOADS=never

# Install system packages
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -y --fix-missing \
  && apt-get install -y \
    apt-utils \
    locales \
    ca-certificates \
    && apt-get upgrade -y \
    && apt-get clean

# UTF-8
RUN localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG=en_US.utf8
ENV LC_ALL=C

# Install needed packages
RUN apt-get update -y --fix-missing \
  && apt-get upgrade -y \
  && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    unzip \
    wget \
    curl \
    zip \
    zlib1g \
    zlib1g-dev \
    gnupg \
    rsync \
    python3-pip \
    python3-venv \
    git \
    sudo \
    libglib2.0-0 \
    socat \
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libpng-dev \
    libffi-dev \
    libsm6 \
    libxext6 \
    libxrender1 \
    xdg-utils \
  && apt-get clean

# Add libEGL ICD loaders and libraries + Vulkan ICD loaders and libraries
# Per https://github.com/mmartial/ComfyUI-Nvidia-Docker/issues/26
RUN apt-get install -y --no-install-recommends libglvnd0 libglvnd-dev libegl1-mesa-dev libvulkan1 libvulkan-dev ffmpeg \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && mkdir -p /usr/share/glvnd/egl_vendor.d \
  && echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_nvidia.so.0"}}' > /usr/share/glvnd/egl_vendor.d/10_nvidia.json \
  && mkdir -p /usr/share/vulkan/icd.d \
  && echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libGLX_nvidia.so.0","api_version":"1.3"}}' > /usr/share/vulkan/icd.d/nvidia_icd.json
ENV MESA_D3D12_DEFAULT_ADAPTER_NAME="NVIDIA"

ENV BUILD_FILE="/etc/image_base.txt"
ARG BASE_DOCKER_FROM
RUN echo "DOCKER_FROM: ${BASE_DOCKER_FROM}" | tee ${BUILD_FILE}
RUN echo "CUDNN: ${NV_CUDNN_PACKAGE_NAME} (${NV_CUDNN_VERSION})" | tee -a ${BUILD_FILE}

ARG BUILD_BASE="unknown"
LABEL comfyui-nvidia-docker-build-from=${BUILD_BASE}
RUN it="/etc/build_base.txt"; echo ${BUILD_BASE} > $it && chmod 555 $it

LABEL org.opencontainers.image.source="https://github.com/ethanfel/ComfyUI-Nvidia-Docker" \
      org.opencontainers.image.description="ComfyUI NVIDIA container with a Python 3.13 / CUDA 13.2 Blackwell target" \
      org.opencontainers.image.licenses="MIT"

# Place the init script and its config in / so it can be found by the entrypoint
COPY --chmod=555 init.bash /comfyui-nvidia_init.bash
COPY --chmod=555 config.sh /comfyui-nvidia_config.sh

##### ComfyUI preparation
# Every sudo group user does not need a password
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Create a new group for the comfy and comfytoo users
RUN groupadd -g 1024 comfy \ 
    && groupadd -g 1025 comfytoo

# The comfy (resp. comfytoo) user will have UID 1024 (resp. 1025), 
# be part of the comfy (resp. comfytoo) and users groups and be sudo capable (passwordless) 
RUN useradd -u 1024 -d /home/comfy -g comfy -s /bin/bash -m comfy \
    && usermod -G users comfy \
    && adduser comfy sudo
RUN useradd -u 1025 -d /home/comfytoo -g comfytoo -s /bin/bash -m comfytoo \
    && usermod -G users comfytoo \
    && adduser comfytoo sudo

ENV COMFYUSER_DIR="/comfy"
RUN mkdir -p ${COMFYUSER_DIR}
RUN it="/etc/comfyuser_dir"; echo ${COMFYUSER_DIR} > $it && chmod 555 $it

ENV NVIDIA_DRIVER_CAPABILITIES="all"
ENV NVIDIA_VISIBLE_DEVICES=all

EXPOSE 8188

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

ARG COMFYUI_NVIDIA_DOCKER_VERSION="unknown"
LABEL comfyui-nvidia-docker-build=${COMFYUI_NVIDIA_DOCKER_VERSION}
RUN echo "COMFYUI_NVIDIA_DOCKER_VERSION: ${COMFYUI_NVIDIA_DOCKER_VERSION}" | tee -a ${BUILD_FILE}

# We start as comfytoo and will switch to the comfy user AFTER the container is up
# and after having altered the comfy details to match the requested UID/GID
USER comfytoo

# We use ENTRYPOINT to run the init script (from CMD)
ENTRYPOINT [ "/comfyui-nvidia_init.bash" ]
