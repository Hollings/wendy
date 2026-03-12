# Infrastructure

## Architecture

```
Internet → Lightsail VPS (Caddy, SSL) → Tailscale VPN → Orange Pi (Docker)
           ec2-user@44.255.209.109                       ubuntu@100.120.250.100
```

- **Lightsail VPS** (`44.255.209.109`): Caddy reverse proxy + TLS termination
- **Orange Pi** (`100.120.250.100`): All Docker workloads — wendy bot, wendy-web, game containers

## SSH Access

```bash
# Orange Pi (all Docker services)
ssh ubuntu@100.120.250.100

# Lightsail VPS (Caddy config, DNS, SSL)
ssh -i ~/.ssh/lightsail-west-2.pem ec2-user@44.255.209.109
```

## Caddy Config (wendy.monster routing)

Caddyfile lives on the Lightsail VPS at `/etc/caddy/Caddyfile`.

```bash
# Edit
ssh -i ~/.ssh/lightsail-west-2.pem ec2-user@44.255.209.109
sudo vi /etc/caddy/Caddyfile
sudo systemctl reload caddy

# Tail Caddy logs
sudo journalctl -u caddy -f
```

### wendy.monster block

All wendy.monster traffic should route to wendy-web (port 8910) on the Orange Pi via Tailscale:

```caddy
wendy.monster {
    reverse_proxy 100.120.250.100:8910
}
```

This single `reverse_proxy` directive covers:
- `/` — brain feed (React UI)
- `/game/*` — game container proxy (wendy-web handles internal routing to game containers)
- `/<site>/*` — deployed static sites
- `/api/*` — wendy-web API
- `/ws/brain` — WebSocket brain feed

**Do NOT add separate per-game port entries.** wendy-web proxies all `/game/` traffic
internally by container name — Caddy should never talk directly to game containers.

## Troubleshooting

### 502 on a path

If a path returns 502 from Caddy, check:
1. `curl http://100.120.250.100:8910/<path>` from Lightsail — if this works, Caddy config is wrong
2. `docker logs wendy-web --tail=20` on Orange Pi — if the request doesn't appear, Caddy isn't forwarding it
3. `sudo journalctl -u caddy -f` on Lightsail for Caddy-side errors

### SSL cert issues

```bash
# On Lightsail
sudo rm -rf /var/lib/caddy/.local/share/caddy/*
sudo systemctl restart caddy
sudo journalctl -u caddy -f
```
