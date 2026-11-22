# One container: FastAPI serves the API, Plivo webhooks/media WebSocket, and the built dashboard.
# Built for Hugging Face Spaces (port 7860, uid 1000); runs anywhere Docker does.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DB_PATH=/tmp/recovery.db \
    PORT=7860

WORKDIR /home/user/app/backend
COPY --chown=user backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project
COPY --chown=user backend/app ./app
COPY --chown=user --from=web /web/dist /home/user/app/frontend/dist

EXPOSE 7860
CMD ["sh", "-c", "exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
