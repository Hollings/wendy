# Wendy v2 - Single-process Discord bot
FROM ubuntu:24.04 AS beads-icu
# BD 0.63.3's embedded-Dolt release links ICU 74. Keep its versioned
# libraries alongside Debian's own ICU; do not replace system libraries.
RUN apt-get update && apt-get install -y --no-install-recommends libicu74 \
    && mkdir /icu && cp -a /usr/lib/*-linux-gnu/libicu*.so.74* /icu/

FROM python:3.12-slim

COPY --from=beads-icu /icu/ /usr/local/lib/
RUN ldconfig

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
# Claude CLI is pinned: the API gates new models on a minimum CLI version
# (Opus 5.5 needs >= 2.1.280) and an unpinned install silently stays on
# whatever this layer was last built with. Bump the pin to pick up a newer CLI;
# the change also busts the layer cache so the rebuild actually installs it.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code@2.1.280 @beads/bd@0.63.3 playwright \
    && bd --version \
    && rm -rf /var/lib/apt/lists/*

# Playwright Chromium
RUN npx playwright install chromium

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    pytest ruff numpy imageio matplotlib Pillow requests

# Non-root user for CLI subprocess isolation
RUN useradd -m -s /bin/bash -u 1000 wendy

# Preserve the npm entry point separately from the deprecated public bd shim.
# Resolve its symlink so installing bin/bd cannot overwrite the real backend.
RUN mkdir -p /usr/local/libexec \
    && ln -s "$(readlink -f "$(command -v bd)")" /usr/local/libexec/wendy-bd \
    && /usr/local/libexec/wendy-bd --version

# Copy application code
COPY . .

# Entrypoint
RUN chmod +x /app/deploy/entrypoint.sh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]

# Single process - replaces bot + proxy + orchestrator
CMD ["python", "-m", "wendy"]
