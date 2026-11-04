FROM ghcr.io/jdx/mise:2026.11.0@sha256:6e05da4113a812cc8ac72b21a38889d019fb0f97df6ce8075c0de98d1cf48cc4 AS mise

FROM debian:13-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

ARG BUILD_VERSION=0.0.1
ARG BUILD_ARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git libatomic1 libstdc++6 openssh-client xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

ENV MISE_DATA_DIR=/opt/mise \
    MISE_SHARED_INSTALL_DIRS=/data/mise \
    MISE_TRUSTED_CONFIG_PATHS=/data/:/opt/opencode-addon/

WORKDIR /opt/opencode-addon
COPY docker.mise.toml /etc/mise/config.toml
COPY requirements.txt /opt/opencode-addon/requirements.txt
RUN mkdir -p ${MISE_SHARED_INSTALL_DIRS} /opt/opencode-addon \
    && mise x -- pip install -r requirements.txt

# We copy this after, so that we dont have this in the container
# the tools will be stored in `/data/mise`, and can be reused by
# future incarnations as long as the version of the tool is the same.
COPY mise.toml /opt/opencode-addon/mise.toml

LABEL io.hass.name="OpenCode" \
      io.hass.description="Run one OpenCode instance for each Home Assistant user." \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}"

COPY main.py frontend.py supervisor.py ./
RUN chmod -R go-w /opt/opencode-addon

EXPOSE 8080
ENTRYPOINT /usr/bin/bash -c "echo 'Installing dependencies...'; /usr/local/bin/mise install --quiet --include-task-tools --include-lazy --shared ${MISE_SHARED_INSTALL_DIRS} && /usr/local/bin/mise ls --all-sources && echo 'launching entrypoint' && /usr/local/bin/mise x --quiet -- python /opt/opencode-addon/main.py"
CMD []
