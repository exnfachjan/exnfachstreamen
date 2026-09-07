# Server setup - Oracle Cloud Always Free (Ampere A1)

> On a Hetzner dedicated box (or any Intel/amd64 server) use
> [setup-hetzner.md](setup-hetzner.md) instead - different architecture,
> different firewall model, and an iGPU worth using.

## 1. Create the instance

1. Oracle Cloud console → Compute → Instances → **Create instance**.
2. Image: **Ubuntu 24.04** (aarch64). Shape: **Ampere → VM.Standard.A1.Flex**,
   **2 OCPUs / 12 GB RAM** (the full Always Free allowance as of 2026 - Oracle
   used to offer 4 OCPUs/24 GB, that's gone; claim what's left, and retry
   different availability domains if "out of capacity"). See the sizing note
   in §6 - 2 OCPUs is tight for this stack, not comfortable.
3. Add your SSH public key. Note the **public IP** after creation.

## 2. Open the firewall - BOTH layers

Oracle has two firewalls and both will silently drop your ingest if you forget one.

### 2a. VCN Security List (cloud side)

Networking → your VCN → Security Lists → Default Security List → **Add Ingress Rules**:

| Source | Protocol | Dest. port | Purpose |
|---|---|---|---|
| 0.0.0.0/0 | UDP | 5000 | SRTLA bonded ingest (primary) |
| 0.0.0.0/0 | UDP | 4001 | direct SRT ingest (fallback) |
| 0.0.0.0/0 | TCP | 1935 | RTMP ingest (fallback) |
| 0.0.0.0/0 | TCP | 80  | HTTP → HTTPS redirect |
| 0.0.0.0/0 | TCP | 443 | dashboard + noVNC |
| 0.0.0.0/0 | UDP | 443 | HTTP/3 (optional) |

### 2b. iptables on the instance (the classic Oracle gotcha)

Oracle's Ubuntu images ship a restrictive iptables ruleset that **rejects
everything except SSH even after you fix the Security List**. On the server:

```bash
sudo iptables -I INPUT 6 -p udp --dport 5000 -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 4001 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 1935 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 80   -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 443  -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 443  -j ACCEPT
sudo netfilter-persistent save
```

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in
```

## 4. Deploy

```bash
git clone <your repo url> exnfachstreamen && cd exnfachstreamen
cp .env.example .env
nano .env          # fill in every value (see below)
docker compose build   # OBS compiles from source: 20–40 min first time
docker compose up -d
docker compose logs -f watchdog   # watch it bootstrap the OBS scenes
```

`.env` notes:
- `PUBLISH_KEY`, `RESTREAM_KEY`, `OBS_WS_PASSWORD`: `openssl rand -hex 16`
- `DASH_PASS_HASH`: `docker run --rm caddy caddy hash-password --plaintext YOUR_PASSWORD`
  (paste the bcrypt output, and escape every `$` in it as `$$` - see the note
  in `.env.example`). Plain `docker run`, **not** `docker compose run`: the
  latter also starts everything caddy `depends_on`, which cascades into the
  full OBS build just to hash a password.

## 5. First-run checklist

1. `https://<your-domain>/` → log in. `DOMAIN` in `.env` is what Caddy serves
   on and requests the Let's Encrypt certificate for, so this is the domain,
   not the bare IP (see "Running without a domain" below if you don't have one
   yet).
2. Add at least one Twitch channel under "Twitch destinations" (Twitch →
   Creator Dashboard → Settings → Stream, copy the Primary Stream key). Add
   more if you want to simulcast to several channels at once.
3. Upload a BRB video or image.
4. Configure your phone app (`docs/phone-setup.md`) and start it - the
   **Ingest** card should go green.
5. **Start stream** → check your Twitch dashboard(s).
6. Test the failsafe: toggle airplane mode on the phone. Twitch should show
   your BRB within ~2 s and return to live a few seconds after you reconnect -
   with the stream and VOD unbroken.

## TLS and the domain

Point an A record at the public IP and set `DOMAIN=` in `.env` - that's the
whole setup. `caddy/Caddyfile` reads it as its site address and Caddy handles
the Let's Encrypt certificate itself over the ACME HTTP-01 challenge on :80,
so ports 80 and 443 have to be reachable from the internet.

Keep that record **DNS-only (grey cloud)** if it's on Cloudflare: it's the same
host your phone publishes SRT/SRTLA to, and those are UDP, which an
orange-cloud proxy drops.

After changing `DOMAIN`, use `docker compose up -d caddy` - plain `restart`
reuses the container's old environment and won't pick the new value up.

### Running without a domain

Edit `caddy/Caddyfile` per the comment at its top: replace the `{$DOMAIN}` site
address with `:443` and add `tls internal` as the first line inside the block.
You then reach the panel at `https://<public-ip>/` with a browser cert warning
(Caddy's own CA - still encrypted, just not publicly trusted). Both edits are
needed; `:443` alone leaves Caddy with no certificate to serve.

## Sizing / performance

- **2 OCPUs is tight, not comfortable.** OBS's own compositing is
  software-rendered (`LIBGL_ALWAYS_SOFTWARE=1` - no GPU on Ampere), and that
  alone can peg close to both cores at idle, before any encoding happens.
  `obs/config/profile-basic.ini` and `watchdog/watchdog.py`'s `CANVAS_W`/
  `CANVAS_H` are both set to 1280x720 (matching the actual stream output) for
  exactly this reason - compositing at a higher canvas than you ever encode
  just burns CPU on a downscale nobody sees. Don't raise the canvas
  resolution above the output resolution on a 2-OCPU box. Check load with
  `docker stats` and `uptime` (load average should stay under ~2.0) if
  streams are stalling or dropping frames.
- Egress: a 6 Mbps output ≈ 2 TB over ~740 h/month - far inside Oracle's
  10 TB free allowance.
