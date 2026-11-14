# Home Assistant version the tests run against, and the matching
# pytest-homeassistant-custom-component release (each plugin release pins
# exactly one Home Assistant version). Defaults to the integration's
# minimum supported version (hacs.json).
ARG HA_VERSION=2026.10.1
FROM ghcr.io/home-assistant/home-assistant:${HA_VERSION}

ARG PHACC_VERSION=0.13.372
RUN pip install --no-cache-dir pytest-homeassistant-custom-component==${PHACC_VERSION}

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "pytest"]
