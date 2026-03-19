# Infrastructure

## Architecture

```
Internet → VPS (Caddy, SSL) → VPN → Docker Host (Docker)
```

- **VPS**: Caddy reverse proxy + TLS termination
- **Docker Host**: All Docker workloads -- wendy bot, wendy-web, game containers

## SSH Access

Set `DEPLOY_HOST` for the Docker host. VPS access depends on your cloud provider.

```bash
# Docker host (all Docker services)
ssh $DEPLOY_HOST

# VPS (Caddy config, DNS, SSL)
ssh <your-vps-user>@<your-vps-ip>
```

## Caddy Config (wendy.monster routing)

Caddyfile lives on the VPS at `/etc/caddy/Caddyfile`.

```bash
sudo vi /etc/caddy/Caddyfile
sudo systemctl reload caddy

# Tail Caddy logs
sudo journalctl -u caddy -f
```

### wendy.monster block

All wendy.monster traffic should route to wendy-web (port 8910) on the Docker host:

```caddy
wendy.monster {
    reverse_proxy <docker-host-ip>:8910
}
```

This single `reverse_proxy` directive covers:
- `/` -- brain feed (React UI)
- `/game/*` -- game container proxy (wendy-web handles internal routing to game containers)
- `/<site>/*` -- deployed static sites
- `/api/*` -- wendy-web API
- `/ws/brain` -- WebSocket brain feed

**Do NOT add separate per-game port entries.** wendy-web proxies all `/game/` traffic
internally by container name -- Caddy should never talk directly to game containers.

## Troubleshooting

### 502 on a path

If a path returns 502 from Caddy, check:
1. `curl http://<docker-host-ip>:8910/<path>` from the VPS -- if this works, Caddy config is wrong
2. `docker logs wendy-web --tail=20` on Docker host -- if the request doesn't appear, Caddy isn't forwarding it
3. `sudo journalctl -u caddy -f` on VPS for Caddy-side errors

### SSL cert issues

```bash
# On VPS
sudo rm -rf /var/lib/caddy/.local/share/caddy/*
sudo systemctl restart caddy
sudo journalctl -u caddy -f
```
