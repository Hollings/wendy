# Secrets Management

```bash
python3 /data/wendy/secrets.py set github_pat "ghp_xxx"   # Store a secret
python3 /data/wendy/secrets.py get github_pat              # Retrieve a secret
python3 /data/wendy/secrets.py list                        # List all keys (not values)
python3 /data/wendy/secrets.py delete old_key              # Delete a secret
```

Secrets persist across restarts and deployments. Never store secrets in plain text files, CLAUDE.md, or anywhere else — always use this tool.

The container is transient — files at arbitrary paths (including ~/.ssh/) are not guaranteed to persist between sessions. secrets.py is the only reliable storage.
