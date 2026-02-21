# ── Stage 1: build the React brain UI ────────────────────────────────────────
FROM node:20-slim AS brain-builder

WORKDIR /build

COPY services/web/brain-ui/package*.json ./
RUN npm ci

COPY services/web/brain-ui/ ./
# Build to ./dist (overrides the local-dev outDir in vite.config.js)
RUN npx vite build --outDir ./dist --emptyOutDir


# ── Stage 2: Python web service ───────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# curl for healthcheck + Docker CLI for games manager
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY services/web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/web/ .

# Inject the React build output so FastAPI can serve it
COPY --from=brain-builder /build/dist/ ./static/brain/

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
