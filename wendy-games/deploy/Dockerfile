FROM python:3.12-slim

WORKDIR /app

# Install Docker CLI for managing game containers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy manager code
COPY manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY manager/main.py .

# Create data directory
RUN mkdir -p /data/games

EXPOSE 8920

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8920"]
