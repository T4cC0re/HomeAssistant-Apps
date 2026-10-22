ARG BUILD_VERSION
FROM ghcr.io/berriai/litellm:v${BUILD_VERSION}

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.name="LiteLLM" \
      io.hass.description="Run the LiteLLM proxy with a YAML configuration." \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}"

COPY run.py /opt/litellm-addon/

ENTRYPOINT ["/app/.venv/bin/python", "/opt/litellm-addon/run.py"]
CMD []
