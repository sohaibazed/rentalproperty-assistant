# Rental Agent web UI. Build and run with `make up`; see docker-compose.yml.
FROM python:3.13-slim-bookworm

# tzdata lets TZ pick the scheduler's local day.
# The final cleanup only empties apt's package-list cache inside this image layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tzdata ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first so code edits don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY templates ./templates
COPY prompts ./prompts
COPY config ./config
COPY agent-plugin ./agent-plugin
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 agent \
 && mkdir -p /data /app/artifacts /app/reference /app/memory \
 && chown agent /data /app/artifacts /app/memory
USER agent

ENV PATH=/app/.venv/bin:$PATH \
    RENTAL_DATA_DIR=/data \
    RENTAL_ARTIFACTS_DIR=/app/artifacts \
    RENTAL_REFERENCE_DIR=/app/reference \
    RENTAL_EMAIL_SOURCES=/app/config/email_sources.toml \
    RENTAL_MODELS_FILE=/app/config/models.toml \
    RENTAL_MEMORY_FILE=/app/memory/memory.md

EXPOSE 8080
# The check only asks "is the server answering", from inside the container, so it does not
# verify the self-signed certificate.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import ssl, urllib.request; c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE; urllib.request.urlopen('https://127.0.0.1:8080/login', context=c)"
# HTTPS only. The certificate and its key are Docker secrets; `make up` creates them.
CMD ["uvicorn", "rental_agent.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--ssl-certfile", "/run/secrets/rental_tls_cert", "--ssl-keyfile", "/run/secrets/rental_tls_key"]
