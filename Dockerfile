# One service: the orchestrator, all five agents (in-process) and the operations console.
# Same origin for UI and API, so the HttpOnly SameSite=Strict session cookie just works.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The app runs as a non-root user and writes runtime state (out/, agent request stores)
# under /app, so /app itself must be owned by that user, not just the files copied in.
RUN useradd --create-home --uid 10001 app && mkdir /app && chown app:app /app
WORKDIR /app

# Dependencies first so code edits don't bust the install layer.
COPY requirements.txt ./
COPY agents/receiving/requirements.txt agents/receiving/requirements.txt
RUN pip install -r requirements.txt

COPY --chown=app:app . .

# Shown on /health and in the console footer so a deployed build can be verified.
ARG GIT_COMMIT=unknown
ARG APP_VERSION=dev
ENV GIT_COMMIT=${GIT_COMMIT} \
    APP_VERSION=${APP_VERSION} \
    PORT=8100

# Non-secret agent configuration (relative to /app). The image ships in DEMO MODE: the agents
# use their local deterministic providers on the bundled sample data, no live model calls.
# /health and the console label this. To use live models set the *_DEMO_MODE variables to 0
# and provide GEMINI_API_KEY (and GEMINI_FREE_TIER_CONFIRMED=1) as host secrets.
ENV LOG_LEVEL=INFO LOG_FORMAT=json \
    RECEIVING_DEMO_MODE=1 PREP_DEMO_MODE=1 RETURNS_DEMO_MODE=1 PACK_DEMO_MODE=1 \
    PACK_CONFIG=data/input/pack_config.json PACK_STATE_DIR=data/input/pack \
    PREP_CONFIG=data/input/prep_config.json PREP_STATE_DIR=data/input/prep \
    RECOVERY_CONFIG=data/input/recovery_config.json RECOVERY_STATE_DIR=data/input/recovery \
    RETURNS_CONFIG=data/input/returns_live_config.json RETURNS_STATE_DIR=data/input/returns_live/state \
    RECEIVING_GEMINI_MODEL=gemini-3.8-flash RECEIVING_TIMEOUT_S=45 \
    RECEIVING_CAPTURE_REGISTRY=agents/receiving/fixtures/captures.json \
    RECEIVING_STATE_DIR=out/receiving RECEIVING_CACHE_DIR=out/receiving-cache

USER app
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# --proxy-headers: behind Render/Fly/Heroku TLS terminates at the proxy; without this the
# app sees http:// and would not mark the session cookie Secure.
CMD ["sh", "-c", "exec uvicorn orchestration.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
