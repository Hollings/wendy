#!/bin/bash
set -e

SERVICE_NAME="wendy-avatar"
REMOTE_HOST="ubuntu@100.120.250.100"
REMOTE_DIR="/srv/$SERVICE_NAME"

echo "Deploying $SERVICE_NAME..."

# Create tarball
cd "$(dirname "$0")"
tar --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='node_modules' \
    -czf /tmp/$SERVICE_NAME.tar.gz \
    index.html src/ styles/ assets/ deploy/

# Upload
scp /tmp/$SERVICE_NAME.tar.gz $REMOTE_HOST:/tmp/

# Deploy
ssh $REMOTE_HOST << EOF
    set -e
    mkdir -p $REMOTE_DIR
    tar -xzf /tmp/$SERVICE_NAME.tar.gz -C $REMOTE_DIR
    cd $REMOTE_DIR/deploy
    docker compose -p $SERVICE_NAME down 2>/dev/null || true
    docker compose -p $SERVICE_NAME up -d --build
    echo "Deployed successfully!"
EOF

rm /tmp/$SERVICE_NAME.tar.gz
echo "Done! Avatar available at port 8915"
