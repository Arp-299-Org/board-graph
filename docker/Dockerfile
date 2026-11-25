FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    KICAD_PYTHON=/usr/bin/python3 \
    KICAD_IPC_PYTHON=/opt/board-graph/.venv-kicad-ipc/bin/python \
    LIBGL_ALWAYS_SOFTWARE=1 \
    NO_AT_BRIDGE=1 \
    XDG_RUNTIME_DIR=/tmp/runtime-root \
    PATH="/opt/board-graph/.venv-kicad-ipc/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dbus-x11 \
    git \
    gnupg \
    imagemagick \
    libgl1-mesa-dri \
    openbox \
    python3 \
    python3-pip \
    python3-venv \
    rsync \
    software-properties-common \
    xdotool \
    xvfb \
    && add-apt-repository -y ppa:kicad/kicad-10.0-releases \
    && curl -sSL -O https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb \
    && dpkg -i packages-microsoft-prod.deb \
    && rm -f packages-microsoft-prod.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends kicad kicad-symbols kicad-footprints powershell \
    && test -f /usr/share/kicad/template/sym-lib-table \
    && test -f /usr/share/kicad/template/fp-lib-table \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/board-graph
COPY scripts/kicad-ipc-requirements.txt /opt/board-graph/scripts/kicad-ipc-requirements.txt

RUN /usr/bin/python3 -m venv /opt/board-graph/.venv-kicad-ipc \
    && /opt/board-graph/.venv-kicad-ipc/bin/pip install --no-cache-dir setuptools wheel -r /opt/board-graph/scripts/kicad-ipc-requirements.txt

COPY . /opt/board-graph

RUN /opt/board-graph/.venv-kicad-ipc/bin/pip install --no-cache-dir --no-deps --no-build-isolation -e /opt/board-graph \
    && chmod +x /opt/board-graph/docker/entrypoint.sh /opt/board-graph/scripts/mac_screen.sh

ENTRYPOINT ["/opt/board-graph/docker/entrypoint.sh"]
CMD ["python3", "/opt/board-graph/docker/smoke_linux_ipc.py"]
