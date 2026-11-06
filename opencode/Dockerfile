FROM ghcr.io/jdx/mise:2026.11.0@sha256:6e05da4113a812cc8ac72b21a38889d019fb0f97df6ce8075c0de98d1cf48cc4 AS mise

FROM ubuntu:26.04

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH

COPY --from=mise /usr/local/bin/mise /usr/bin/mise

ENV MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/etc/mise \
    MISE_TRUSTED_CONFIG_PATHS=/opt/opencode-addon/

WORKDIR /opt/opencode-addon
COPY mise.toml /etc/mise/config.toml
COPY requirements.txt /opt/opencode-addon/requirements.txt
# Install shared tools before the runtime filesystem becomes read-only.
RUN mkdir -p /opt/opencode-addon /opt/mise /home/opencode \
    && mise bootstrap \
    && mise x -- pip install -r requirements.txt

LABEL io.hass.name="OpenCode" \
      io.hass.description="Run multi-tenanot OpenCode (instance for each Home Assistant user)" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}"

COPY main.py frontend.py supervisor.py sandbox.py ./
RUN chmod -R go-w /opt/opencode-addon

EXPOSE 8080
ENTRYPOINT /usr/bin/bash -c "echo 'launching entrypoint' && mise x --quiet -- python /opt/opencode-addon/main.py"
CMD []
