FROM ghcr.io/home-assistant/home-assistant:2026.9.3

# The plugin version tracks the Home Assistant release it is built for.
RUN pip install --no-cache-dir pytest-homeassistant-custom-component==0.13.366

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "pytest"]
