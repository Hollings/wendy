## Secrets Management

**ALL sensitive data (API keys, tokens, passwords, SSH keys) MUST be stored via secrets.py.**

This Docker container is transient - files at arbitrary paths (like ~/.ssh/) are lost when sessions fork or containers restart. secrets.py is the ONLY reliable way to persist sensitive data.

```bash
python3 /data/wendy/secrets.py set <key> "<value>"    # Store a secret
python3 /data/wendy/secrets.py get <key>               # Retrieve a secret
python3 /data/wendy/secrets.py list                    # List all keys (not values)
python3 /data/wendy/secrets.py delete <key>            # Delete a secret
```

**Auto-backup:** Every time a secret is saved, secrets.py automatically backs up ALL secrets to the Pi at `/workbench/wendy-credentials/secrets_backup.json` via SFTP.

**CRITICAL: pi-ssh.py auto-restore.** The Pi SSH key is stored as `pi_ssh_key` in secrets.py. When pi-ssh.py connects, it checks if `/root/.ssh/id_ed25519` exists on disk - if not, it automatically restores it from secrets.py. This means SSH to the Pi works even after session forks without manual intervention.
