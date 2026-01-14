#!/bin/bash
# Deploy wendy-games to Orange Pi
set -euo pipefail

SERVICE_NAME="wendy-games"
REMOTE_HOST="ubuntu@100.120.250.100"
REMOTE_DIR="/srv/${SERVICE_NAME}"

echo "Deploying ${SERVICE_NAME}..."

# Create tarball
cd "$(dirname "$0")"
tar --exclude='node_modules' --exclude='.git' --exclude='__pycache__' \
    -czf /tmp/${SERVICE_NAME}.tar.gz .

# Upload
scp /tmp/${SERVICE_NAME}.tar.gz ${REMOTE_HOST}:/tmp/

# Deploy on remote
ssh ${REMOTE_HOST} << 'ENDSSH'
set -euo pipefail

SERVICE_NAME="wendy-games"
REMOTE_DIR="/srv/${SERVICE_NAME}"

# Extract
mkdir -p ${REMOTE_DIR}
tar -xzf /tmp/${SERVICE_NAME}.tar.gz -C ${REMOTE_DIR}
rm /tmp/${SERVICE_NAME}.tar.gz

# Ensure .env exists
if [ ! -f "${REMOTE_DIR}/deploy/.env" ]; then
    echo "Creating .env from example..."
    cp "${REMOTE_DIR}/deploy/.env.example" "${REMOTE_DIR}/deploy/.env"
    echo "WARNING: Update ${REMOTE_DIR}/deploy/.env with your deploy token!"
fi

cd ${REMOTE_DIR}/deploy

# Build runtime image first
echo "Building runtime image..."
docker compose --profile build build runtime-builder

# Start manager
echo "Starting manager..."
docker compose up -d --build manager

echo "Deployment complete!"
docker compose ps
ENDSSH

rm /tmp/${SERVICE_NAME}.tar.gz
echo "Done!"
