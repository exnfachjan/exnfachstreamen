# Cloudflare Tunnel

The panel (dashboard + noVNC) is reached only through a Cloudflare Tunnel.
The `cloudflared` container opens an outbound connection to Cloudflare; no
web port is published on the server, so 80 and 443 stay closed.

```
browser ──https──► Cloudflare ══tunnel══► cloudflared ──http──► caddy:80 ──► dashboard / obs (noVNC)
phone   ──UDP/TCP (SRTLA 5000, SRT 4001, RTMP 1935)──────────────────────► ingest   (direct, not tunnelled)
```

Caddy stays, but only as the internal login gate (`forward_auth` against the
dashboard session, which also protects noVNC). It no longer handles TLS or
certificates.

## Why two hostnames

A tunnel carries HTTP(S) only. SRT and SRTLA are UDP, so the phone ingest
cannot go through it - it needs its own **DNS-only (grey cloud)** record that
points at the server's real IP.

| `.env` key | Example | DNS record | Used for |
|---|---|---|---|
| `DOMAIN` | `stream.example.com` | CNAME, proxied - created by the tunnel | panel |
| `INGEST_HOST` | `ingest.example.com` | A → server IP, **grey cloud** | phone / encoder |

## Setup (about 5 minutes)

1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel**
   → type **Cloudflared** → any name.
2. On the "Install connector" page copy the token (the long string after
   `--token`). Ignore the install command itself - the stack runs the
   connector. Put the token in `.env` as `TUNNEL_TOKEN` (`install.sh` and
   `update.sh` ask for it if it is missing).
3. **Public Hostname** tab → Add:
   - Subdomain/Domain: your `DOMAIN`
   - Service: **HTTP**, URL **`caddy:80`**
4. DNS → add an **A** record for `INGEST_HOST` → server IP, **proxy off**.
5. `docker compose up -d` (or `sudo ./update.sh`), then check:

   ```bash
   docker compose logs cloudflared | grep -i "registered tunnel connection"
   ```

   Four "Registered tunnel connection" lines = connected.

## Migrating an existing server

Run `sudo ./update.sh`. It asks for `TUNNEL_TOKEN` and `INGEST_HOST`, removes
the published 80/443 from Caddy and starts `cloudflared`.

Afterwards:

- The old A record of `DOMAIN` is replaced by the tunnel's CNAME when you add
  the Public Hostname (Cloudflare asks to overwrite it - say yes).
- Re-point the phone at `INGEST_HOST`. The dashboard's "Connect your encoder"
  card shows the new URLs.
- Close 80/443 in any firewall you opened for them; nothing listens there now.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Cloudflare error 1033 | `cloudflared` not running or wrong token → `docker compose logs cloudflared` |
| Cloudflare 502 | Public Hostname service is not `http://caddy:80` (https:// or another port) |
| Login loop | Hostname set to "No TLS Verify"/HTTPS origin, or a Cloudflare Access app in front that strips cookies |
| Phone cannot connect | `INGEST_HOST` is orange-cloud, or points at the panel hostname |
