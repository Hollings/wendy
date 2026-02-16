# Wendy Bot - Python 3.12 + Node.js + Claude CLI
FROM python:3.12-slim

WORKDIR /app

# System dependencies: rendering, fonts, common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Cairo/Pango for SVG rendering
    libcairo2 libpangocairo-1.0-0 fonts-dejavu-core \
    # Common CLI tools
    curl wget git tmux jq vim-tiny less unzip tar gzip ffmpeg \
    # Build tools (for native npm/pip packages)
    build-essential \
    # Playwright browser dependencies
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI + Compose plugin (client only, no daemon)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

# Install Node.js, Claude Code CLI, and Beads
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code @beads/bd playwright \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright Chromium browser
RUN npx playwright install chromium

# Python dependencies for bot
COPY bot/requirements.txt /tmp/bot-requirements.txt
RUN pip install --no-cache-dir -r /tmp/bot-requirements.txt

# Python dependencies for proxy
COPY proxy/requirements.txt /tmp/proxy-requirements.txt
RUN pip install --no-cache-dir -r /tmp/proxy-requirements.txt

# Common Python dev/data tools
RUN pip install --no-cache-dir \
    pytest ruff \
    numpy imageio matplotlib

# Copy application code
COPY . .

# Make entrypoint executable
RUN chmod +x /app/deploy/entrypoint.sh

# Entrypoint sets up Claude sync hooks before running command
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

# Default command (bot)
CMD ["python", "-m", "bot"]
