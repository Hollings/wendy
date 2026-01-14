# Wendy Bot - Python 3.12 + Node.js + Claude CLI
FROM python:3.12-slim

WORKDIR /app

# System dependencies for CairoSVG rendering + basic fonts + curl + git
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpangocairo-1.0-0 fonts-dejavu-core \
    curl git \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js, Claude Code CLI, and Beads
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code @beads/bd \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies for bot
COPY bot/requirements.txt /tmp/bot-requirements.txt
RUN pip install --no-cache-dir -r /tmp/bot-requirements.txt

# Python dependencies for proxy
COPY proxy/requirements.txt /tmp/proxy-requirements.txt
RUN pip install --no-cache-dir -r /tmp/proxy-requirements.txt

# Copy application code
COPY . .

# Default command (bot)
CMD ["python", "-m", "bot"]
