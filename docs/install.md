# Install guide - fresh Hetzner server to live stream

The shortest path from an empty Hetzner dedicated server to a working stream.
Each step says what to type and what you should see. The *why* behind the
steps is in [setup-hetzner.md](setup-hetzner.md); you do not need it to follow
this page.

**Time:** about 1 hour, of which 10-25 minutes is waiting for the build.

## What you need before you start

| | Where to get it |
|---|---|
| A Hetzner dedicated server | Hetzner Robot. Note its IP and root password. |
| An SSH key on your computer | `ls ~/.ssh/id_ed25519.pub`. None? `ssh-keygen -t ed25519` |
| A domain on Cloudflare | Cloudflare dashboard → Add a site |
| Your Twitch stream key | Twitch Creator Dashboard → Settings → Stream → Primary Stream key |

Pick two hostnames now, both under your Cloudflare domain:

- **Panel**, e.g. `stream.example.com` - dashboard, through the tunnel
- **Ingest**, e.g. `ingest.example.com` - where the phone sends video

---

## 1. Install Ubuntu (10 min)

1. Hetzner Robot → your server → **Rescue** → Linux 64 bit → activate, then
   **Reset** → hardware reset.
2. Log in with the rescue password Robot shows:

   ```bash
   ssh root@<server-ip>
   installimage
   ```

3. Choose **Ubuntu 24.04 LTS** (minimal). In the editor that opens, make sure
   `/` gets at least **60 GB** (all remaining space is best). Save with `F10`,
   confirm, wait.
4. `reboot`. Your computer will warn that the host key changed - expected after
   a reinstall:

   ```bash
   ssh-keygen -R <server-ip>
   ```

## 2. First login and a user account (5 min)

```bash
ssh root@<server-ip>                 # password from installimage / Robot
apt update && apt full-upgrade -y
apt install -y git
```

Put your SSH key on the server, then create your own user. From **your
computer** (not the server):

```bash
ssh-copy-id root@<server-ip>
```

Back on the server, replace `streamer` with the name you want:

```bash
adduser --disabled-password --gecos "" streamer
usermod -aG sudo streamer
passwd streamer                       # needed for sudo
mkdir -p /home/streamer/.ssh
cp /root/.ssh/authorized_keys /home/streamer/.ssh/
chown -R streamer:streamer /home/streamer/.ssh && chmod 700 /home/streamer/.ssh
```

**Test the new login in a second terminal before going on:**

```bash
ssh streamer@<server-ip>
sudo whoami                           # must print: root
```

Only if that worked, shut off root and password logins (still on the root
session):

```bash
cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
sshd -t && systemctl restart ssh
```

From here on, always log in as `streamer`.

## 3. Cloudflare: tunnel and DNS (5 min)

Background and troubleshooting: [cloudflare-tunnel.md](cloudflare-tunnel.md).

1. Cloudflare → **Zero Trust → Networks → Tunnels → Create a tunnel** →
   type **Cloudflared** → name it (e.g. `exnfachstreamen`).
2. On the "Install connector" page, copy only the **token** - the long string
   after `--token`. Keep it for step 5. Do not run the install command shown
   there; the stack runs the connector itself.
3. Next → **Public Hostname**: your panel hostname → Service **HTTP**,
   URL **`caddy:80`** → Save.
4. Cloudflare → your domain → **DNS** → Add record:
   type **A**, name `ingest`, IPv4 = server IP, proxy status **DNS only
   (grey cloud)**.

The grey cloud matters: with the orange cloud the phone cannot connect, because
SRT/SRTLA use UDP and Cloudflare's proxy does not carry it.

## 4. Download the repository (1 min)

As `streamer`:

```bash
cd ~
git clone https://github.com/exnfachjan/exnfachstreamen.git
cd exnfachstreamen
```

**If the repository is private**, `git clone` asks for a password and GitHub
rejects your account password. Use a read-only deploy key instead:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub
```

Paste that line at GitHub → repository → **Settings → Deploy keys → Add**
(leave "Allow write access" off), then:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" \
    git clone git@github.com:exnfachjan/exnfachstreamen.git
cd exnfachstreamen
git config core.sshCommand "ssh -i ~/.ssh/github_deploy"   # so update.sh can pull
```

## 5. Run the installer (30-40 min, mostly waiting)

```bash
sudo ./install.sh
```

It asks, in this order:

| Question | Answer |
|---|---|
| Use the 'hetzner' profile? | **Enter** (yes) |
| Reboot now? | **y** - only asked the first time, to enable the iGPU |

After the reboot, log in again and run the **same command** - it continues
where it stopped:

```bash
ssh streamer@<server-ip>
cd ~/exnfachstreamen && sudo ./install.sh
```

| Question | Answer |
|---|---|
| Panel hostname | `stream.example.com` |
| Dashboard password | Enter = generate one. **Write it down** - it is shown once. |
| Tunnel token | the token from step 3 |
| Ingest hostname | `ingest.example.com` (Enter accepts the suggestion) |
| Resolution | **1080** (60 fps, 7000 kbps) |

Then it builds and starts everything. At the end you see a table where every
service says `running`, plus your dashboard URL.

Check the tunnel is connected:

```bash
sudo docker compose logs cloudflared | grep -c "Registered tunnel connection"   # expect 4
```

## 6. First stream (10 min)

1. Open `https://stream.example.com` → log in as `admin` with the password
   from step 5.
2. **Twitch destinations** → add your channel with its stream key.
3. **BRB media** → upload a video or image shown when the phone drops out.
4. **Connect your encoder** card → click a URL to copy it → put it in the phone
   app (SRTLA for bonding). Phone settings: [phone-setup.md](phone-setup.md).
5. Start the phone stream, then **Start** in the dashboard.
6. Twitch Creator Dashboard → Settings → Stream → Latency mode: **Low latency**.

Done when Twitch shows you live and the dashboard scene is `LIVE`.

---

## Later

| Task | Command (in `~/exnfachstreamen`) |
|---|---|
| Update to the latest version | `sudo ./update.sh` |
| Remove the stack, keep settings | `sudo ./uninstall.sh` |
| Remove everything | `sudo ./uninstall.sh --purge` |
| Status | `sudo docker compose ps` |
| Logs of one service | `sudo docker compose logs -f <service>` (`watchdog`, `ingest`, `cloudflared`, ...) |

`.env` belongs to root (mode 600), which is why every `docker compose`
command here needs `sudo`.

Updates restart containers, so a running stream drops for a few seconds. Run
them between streams.

## When something is wrong

| Symptom | Fix |
|---|---|
| `git clone` asks for a password | Private repository - use the deploy key in step 4 |
| Browser shows Cloudflare error 1033 | Tunnel not connected: `sudo docker compose logs cloudflared`, check the token in `.env` |
| Cloudflare error 502 | Public Hostname must be **HTTP** + `caddy:80` |
| Phone cannot connect | Ingest record must be grey cloud and point at the server IP |
| Dashboard live, Twitch not | `sudo docker compose logs ingest`, check the Twitch key in the dashboard |
| Installer stops with an error | Fix what it says, run `sudo ./install.sh` again - finished steps are skipped |
