FROM ghcr.io/jdx/mise:2026.11.0@sha256:6e05da4113a812cc8ac72b21a38889d019fb0f97df6ce8075c0de98d1cf48cc4 AS mise

FROM debian:13-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4 AS tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git libatomic1 libstdc++6 openssh-client xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

ENV MISE_DATA_DIR=/opt/mise \
    MISE_CONFIG_DIR=/opt/mise-config \
    MISE_CACHE_DIR=/tmp/mise-cache \
    MISE_STATE_DIR=/tmp/mise-state \
    MISE_TRUSTED_CONFIG_PATHS=/opt/opencode-addon \
    MISE_YES=1 \
    PATH=/opt/venv/bin:/opt/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/opencode-addon
COPY mise.toml ./
RUN mkdir -p /opt/mise-config \
    && mise install \
    && mise exec -- python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir aiohttp==3.14.5 \
    && chmod -R a+rX /opt/mise /opt/venv \
    && chmod -R go-w /opt/mise /opt/venv /opt/mise-config \
    && rm -rf /tmp/mise-cache /tmp/mise-state

FROM tools AS frontend

COPY prepare_frontend.py ./
RUN mkdir -p /tmp/opencode-source \
    && curl -fsSL https://codeload.github.com/anomalyco/opencode/tar.gz/refs/tags/v1.19.5 \
        -o /tmp/opencode-source.tar.gz \
    && echo "7d2ac43b5083a3a4687fe23b79ab153b8d93a6fb186fa07209ed28d4731e5c7a  /tmp/opencode-source.tar.gz" \
        | sha256sum --check --strict \
    && tar -xzf /tmp/opencode-source.tar.gz --strip-components=1 -C /tmp/opencode-source \
    && mise exec -- python /opt/opencode-addon/prepare_frontend.py /tmp/opencode-source \
    && /usr/local/bin/mise -C /opt/opencode-addon exec -- \
        bun install --cwd /tmp/opencode-source --ignore-scripts --frozen-lockfile \
    && OPENCODE_CHANNEL=prod /usr/local/bin/mise -C /opt/opencode-addon exec -- \
        bun run --cwd /tmp/opencode-source/packages/app build --base ./ \
    && mv /tmp/opencode-source/packages/app/dist /tmp/opencode-web \
    && rm -rf /tmp/opencode-source /tmp/opencode-source.tar.gz /root/.bun \
        /tmp/mise-cache /tmp/mise-state

FROM tools AS runtime

ARG BUILD_VERSION=0.0.1
ARG BUILD_ARCH
LABEL io.hass.name="OpenCode" \
      io.hass.description="Run one OpenCode instance for each Home Assistant user." \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}"

COPY --from=frontend /tmp/opencode-web ./web/
COPY main.py frontend.py supervisor.py ./
RUN chmod -R go-w /opt/opencode-addon

EXPOSE 8080
ENTRYPOINT ["/opt/venv/bin/python", "/opt/opencode-addon/main.py"]
CMD []
