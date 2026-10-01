FROM node:22-bookworm-slim AS claude
RUN npm install -g @anthropic-ai/claude-code@2.1.280

FROM python:3.12-slim-bookworm
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=claude /usr/local/bin/claude /usr/local/bin/claude
RUN claude --version && useradd --create-home --uid 1000 memory \
    && mkdir -p /data/memory && chown memory:memory /data/memory
WORKDIR /app
COPY services/memory/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY memory_protocol /app/memory_protocol
COPY services/memory /app/services/memory
COPY config/memory_researcher.txt /app/config/memory_researcher.txt
COPY config/memory_limits.json /app/config/memory_limits.json
USER memory
ENV PYTHONPATH=/app WENDY_MEMORY_BIND=0.0.0.0
CMD ["python", "-m", "services.memory.server"]
