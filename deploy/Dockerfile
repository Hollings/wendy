# Wendy v2 - Single-process Discord bot
FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpangocairo-1.0-0 fonts-dejavu-core \
    curl wget git tmux jq vim-tiny less ffmpeg \
    unzip zip tar gzip p7zip-full \
    iputils-ping openssh-client rsync \
    sqlite3 imagemagick \
    build-essential \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (client only, for deploy commands)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# Node.js, Claude Code CLI, Beads
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code @beads/bd@0.41.0 playwright \
    && rm -rf /var/lib/apt/lists/*

# Playwright Chromium
RUN npx playwright install chromium

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    pytest ruff numpy imageio matplotlib Pillow requests

# Non-root user for CLI subprocess isolation
RUN useradd -m -s /bin/bash -u 1000 wendy

# Copy application code
COPY . .

# Entrypoint
RUN chmod +x /app/deploy/entrypoint.sh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

# Single process - replaces bot + proxy + orchestrator
CMD ["python", "-m", "wendy"]
