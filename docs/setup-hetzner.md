# Server setup - Hetzner dedicated, Intel i7-7700

For a Hetzner **dedicated** root server on an i7-7700 (Kaby Lake, 4 cores /
8 threads, Intel HD Graphics 630 iGPU). If you're on Oracle Cloud Ampere
instead, use [setup.md](setup.md) - that one is ARM64 and its firewall and
sizing sections don't apply here at all.

## What's different from the Oracle guide

| | Oracle Ampere | This box |
|---|---|---|
| Architecture | arm64 | **amd64** |
| GPU | none | **Intel HD 630** (VA-API encode, once enabled) |
| CPU budget | 2 OCPUs, tight | 4c/8t, comfortable |
| Firewall | two layers, both fiddly | none by default - **you are wide open on boot** |
| Traffic | 10 TB free tier | unmetered @ 1 Gbit/s |

The repo builds unmodified on amd64: `obs/Dockerfile` already picks its
KasmVNC and Sunshine assets by `TARGETARCH`, and the `sls`/`srtla_rec`
binaries come from a multi-arch upstream image. Nothing needs porting.

---

> **There is an installer now.** `sudo ./install.sh` does sections 2, 4 and 5
> of this document, and does them more carefully than a person following along
> at 3am. This page remains the explanation of *why* each step exists, and the
> reference for anything the installer deliberately leaves to you: the OS
> install below, the firewall decisions in section 3, and the tuning in
> section 7.

## 1. Install the OS

Hetzner Robot → your server → **Rescue** tab → activate Linux rescue (64 bit),
then reset the server. SSH in as `root` with the password Robot showed you.

```bash
installimage
```

In the menu: **Ubuntu 24.04 LTS (Noble) minimal**. In the config editor that
opens afterwards, the defaults are fine for this stack; the two worth a look:

- **Partitioning** - give `/` everything, or at least 60 GB. The OBS source
  build stage alone is several GB, and Docker keeps build cache on top. A
  default 30 GB root will bite you halfway through `docker compose build`.
- **RAID** - leave software RAID1 on if the box has two disks. It costs
  nothing here; this stack is not disk-bound.

Save (`F10`), let it run, `reboot`, then SSH back in on the server's real IP.

```bash
apt update && apt full-upgrade -y && reboot
```

### A non-root user

```bash
adduser --disabled-password --gecos "" jan
usermod -aG sudo jan
mkdir -p /home/jan/.ssh && cp /root/.ssh/authorized_keys /home/jan/.ssh/
chown -R jan:jan /home/jan/.ssh && chmod 700 /home/jan/.ssh
```

Then harden SSH - on a Hetzner IP you will see brute-force attempts within
minutes of boot:

```bash
cat > /etc/ssh/sshd_config.d/10-exnfachstreamen-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sshd -t && systemctl restart ssh
```

A drop-in rather than an edit of `sshd_config`: the `Include` line sits near
the top of that file and sshd takes the first value it sees for a setting, so
a drop-in wins over the defaults further down and survives package upgrades.

**Two things will lock you out if you get them wrong.**

`PermitRootLogin no` assumes the `jan` user above actually exists and works. If
you skipped that step, use `prohibit-password` instead - that still kills the
password brute-force, which is what the bots are actually trying, without
closing the only account you have.

And `PasswordAuthentication no` assumes a key is already deposited. Check
before restarting sshd, not after:

```bash
wc -l /root/.ssh/authorized_keys   # 0 means you are about to lock yourself out
```

Confirm a login **in a second terminal** before you close the first one. A
useful extra check is to prove the password path is really shut:

```bash
ssh -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password \
    root@localhost 2>&1 | tail -1     # expect: Permission denied (publickey).
```

---

## 2. Kernel modules

**`install.sh` does all of this**, including the part below that this section
used to omit entirely.

Two modules this stack wants, neither guaranteed to be loaded on a fresh
minimal install:

```bash
sudo modprobe i915      # the iGPU (see §4)
sudo modprobe uinput    # Sunshine's virtual mouse/keyboard

printf 'i915\nuinput\n' | sudo tee /etc/modules-load.d/exnfachstreamen.conf
```

`uinput` is what `docker-compose.prod.yml` maps into the OBS container. Without
it that file's `devices:` entry fails and the container won't start.

### 2a. Hetzner blocks the iGPU twice, and neither block is obvious

`modprobe i915` on its own is not enough on a Hetzner box, and the way it fails
is actively misleading. installimage ships two separate blocks:

- `/etc/default/grub.d/hetzner.cfg` puts **`nomodeset`** on the kernel command
  line, which stops any KMS driver from probing.
- `/etc/modprobe.d/blacklist-hetzner.conf` **blacklists `i915` and `drm`** under
  the heading "buggy kernel modules".

With `nomodeset` in place the module loads and shows up in `lsmod`, but never
binds: `lspci -k` reports no "Kernel driver in use", `dmesg` has not one i915
line, and `/dev/dri` does not exist. That looks exactly like a BIOS problem,
which is why §4a below used to send people to Hetzner support for nothing.

```bash
grep -E '^[[:space:]]*GRUB_CMDLINE' /etc/default/grub.d/hetzner.cfg
grep -E '^blacklist (i915|drm)' /etc/modprobe.d/blacklist-hetzner.conf
```

Fixing both, regenerating grub and rebooting is what `install.sh --profile
hetzner` does, backing up each file first.

---

## 3. Firewall - read this before you deploy

Hetzner ships **no firewall**. The moment your containers are up, every
published port is reachable from the whole internet. That is intentional for
most of them (a phone has to reach the SRT ingest from anywhere), but two
things need care.

### 3a. Docker bypasses ufw - do not trust it

Docker inserts its own DNAT rules ahead of the `INPUT` chain. `ufw deny 1935`
looks like it worked and does nothing at all. This is not theoretical: the
upstream streamserver was found running as an open RTMP relay with 897
accepted connections behind a `ufw` that appeared correctly configured
(documented in the upstream streamserver project; that file is not part of
this repository).

What actually works for container ports is the `DOCKER-USER` chain, which
Docker leaves alone.

### 3b. Ports that must be public

| Port | Proto | Why |
|---|---|---|
| 5000 | UDP | SRTLA bonded ingest |
| 4001 | UDP | direct SRT ingest |
| 1935 | TCP | RTMP ingest - protected by `on_publish` auth, not by the firewall |
| 80 | TCP | ACME HTTP-01 challenge + redirect |
| 443 | TCP/UDP | dashboard + noVNC |

### 3c. Sunshine is the one you should close

`docker-compose.yml` publishes 47984–47990/tcp, 48010/tcp and 47998–48000/udp
for Sunshine/Moonlight. That's a remote desktop and its own admin web UI,
open to the internet. Unlike everything above it does **not** sit behind
Caddy's login.

**This is already done in this repository** - the obs service's `ports:` block
is commented out, so nothing is published and there is nothing for you to do
here. The rest of this section explains why, and how to undo it.

Upstream publishes those ports, which is easy to read as a considered decision.
It was not one. Upstream ran on Oracle, whose VCN Security List never opened
47984-48000, so Sunshine was unreachable from the internet regardless of what
the compose file said. Hetzner has no such layer in front of the host. The same
file that was harmless there puts a remote desktop straight on your public IP.

You already have browser access to the same OBS desktop through noVNC at
`https://<your-domain>/obs/...`, which *is* behind the dashboard login. So
unless you specifically want the Moonlight client's quality, leave it as it is.

If you do want Moonlight, uncomment that block and restrict it to your own
address - never simply open it:

```bash
sudo iptables -I DOCKER-USER -p tcp --dport 47984:47990 ! -s <your-ip> -j DROP
sudo iptables -I DOCKER-USER -p tcp --dport 48010       ! -s <your-ip> -j DROP
sudo iptables -I DOCKER-USER -p udp --dport 47998:48000 ! -s <your-ip> -j DROP
sudo DEBIAN_FRONTEND=noninteractive apt install -y iptables-persistent
sudo netfilter-persistent save
```

### 3d. The Hetzner Robot firewall - probably skip it

Robot has a free packet filter that sits upstream of the host, so Docker
can't bypass it. Tempting, but it is **stateless** and capped at 10 rules.
Stateless plus UDP means you also have to hand-write rules for return
traffic (DNS replies to high ports, and so on), and a mistake there breaks
your stream rather than your SSH. For this stack the ports that need to be
open are open to the world anyway; `DOCKER-USER` covers the one exception.
Leave Robot's firewall off unless you want it purely to restrict SSH.

---

## 4. The iGPU

Short answer to "muss die wahrscheinlich aktiviert werden?": **maybe, and
you should find out before you care about it** - the i7-7700 handles this
workload in software regardless. Details in §8. Here's how to check.

### 4a. Is the GPU visible to the OS at all?

```bash
lspci -nn | grep -iE 'vga|display'
```

Expect something like `Intel Corporation HD Graphics 630 [8086:5912]`.

- **Shown** → the iGPU is enabled in BIOS, continue to 4b.
- **Not shown** → it really is disabled in BIOS. Check §2a first, though:
  a GPU that *is* listed here but still has no `/dev/dri` is the far more
  common case on Hetzner, and it has nothing to do with the BIOS.
  If it genuinely is not shown, you cannot fix that over SSH.
  Open a Hetzner support ticket and ask them to enable the integrated
  graphics / IGD, or request a KVM console (Robot → server → Support) and do
  it yourself. On the boards Hetzner uses for this line the setting is under
  Advanced → System Agent / Graphics Configuration, named *Primary Display*
  (set to `IGFX`) or *IGD Multi-Monitor* (set to `Enabled`). A headless
  server with no monitor attached is exactly the case where some BIOSes park
  it off by default.

### 4b. Is the render node there?

```bash
ls -l /dev/dri/
```

You want `renderD128` (the compute/encode node - `card0` alone isn't enough).

If `/dev/dri` is missing but `lspci` showed the GPU:

```bash
sudo modprobe i915
dmesg | grep -i i915 | tail -20
sudo apt install -y linux-firmware   # i915 wants the Kaby Lake DMC blob
```

Kaby Lake is old, fully supported hardware - it needs **no** `i915.force_probe`
kernel parameter. That workaround is for GPUs newer than the kernel, and
passing it here won't help.

### 4c. Prove it works inside the container

Only after §5 has built the images. Bring the stack up with the iGPU overlay:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.prod.yml \
               -f docker-compose.igpu.yml up -d
docker compose exec obs vainfo --display drm --device /dev/dri/renderD128
```

**Those arguments are not optional.** Plain `vainfo` picks the X11 backend
first, and inside this container that fails before it ever reaches the render
node:

```
libva error: vaGetDriverNames() failed with unknown libva error
```

That message says nothing about your GPU. It is `vainfo` choosing the wrong
display backend, and following it leads to "fixing" a driver that was never
broken. With `--display drm` libva finds the driver on its own, and no
`LIBVA_DRIVER_NAME` is needed anywhere.

The line that matters is an H.264 profile with an **encode** entrypoint:

```
VAProfileH264High : VAEntrypointEncSlice
```

`VAEntrypointVLD` alone is decode only and won't help you. `VAEntrypointEncSliceLP`
is the low-power path: usable, and all the in-container `iHD` driver offers for
`H264High`, but lower quality per bit than the full path.

**`vainfo` is not the authority anyway - OBS is.** Its log settles the question
directly, because OBS opens the render node itself over DRM and never involves
X11:

```bash
docker compose exec obs sh -c \
  'f=$(ls /config/.config/obs-studio/logs/*.txt | sort | tail -1); grep -i vaapi "$f"'
```

Look for `FFmpeg VAAPI H264 encoding supported` and `ffmpeg_vaapi_tex` in the
encoder list. If those are there, hardware encoding works whatever `vainfo`
said.

### 4d. Switch OBS to the hardware encoder

**Consider not doing this.** §8 explains why: the only encode in the stack is
OBS's single output, and one 720p30 x264 `veryfast` stream is a small fraction
of a 7700. The iGPU buys headroom, not feasibility. You also give something up
by switching - see the end of this section. If you are at 720p and not short of
CPU, x264 is the better default.

If you do want it, the encoder is an OBS profile setting, not a compose setting:

1. Open `https://<your-domain>/obs/vnc.html?path=obs/websockify&autoconnect=true`
2. OBS → Settings → Output → Video Encoder → **FFmpeg VAAPI H.264**
3. Set **Rate Control `CBR`**, your bitrate, and **Keyframe Interval `2`**
4. Apply, then **verify a real stream goes live on Twitch**, not just that OBS
   says "streaming".

### The trap this section used to walk into

Picking a non-default encoder flips Output Mode from **Simple** to
**Advanced**, and that quietly changes where OBS reads its settings from.
`[SimpleOutput]` in `basic.ini` - which is where `VBitrate` and, critically,
`KeyintSec=2` live - stops being read at all. Advanced mode uses `[AdvOut]`
plus a separate `streamEncoder.json` in the profile directory, and OBS writes
only non-default values there. Leave the keyframe field alone and it is simply
absent:

```json
{"bitrate": 8000}
```

The OBS log then reports `bitrate: 0` or a default GOP, and you get exactly the
failure `obs/config/profile-basic.ini` warns about: Twitch accepts the
connection, bytes flow, OBS and the dashboard both show green, and the channel
never goes live with no error anywhere.

Check what actually landed, rather than what the dialog said:

```bash
docker compose exec obs cat \
  /config/.config/obs-studio/basic/profiles/IRL/streamEncoder.json
```

You want `rate_control`, `bitrate` and `keyint_sec` all present. Editing that
file by hand works, but **stop the container first** - OBS holds the profile in
memory and rewrites it on exit, so a live edit is discarded.

### What switching costs you

- **Dynamic bitrate stops working.** VAAPI cannot change bitrate mid-stream, so
  OBS disables the feature: `Dynamic bitrate disabled. The encoder does not
  support on-the-fly bitrate reconfiguration.` x264 keeps it.
- **The zero-copy path does not work here.** OBS renders through llvmpipe, so
  frames arrive in system memory. `ffmpeg_vaapi_tex` logs `Failed to import VA
  surface texture` and falls back to `ffmpeg_vaapi`, which round-trips every
  frame through the CPU. Working, but not free.

### QuickSync is not an option on this build

The Windows tutorials that show "QuickSync H.264" are describing the same
silicon. On Linux, VAAPI *is* the QuickSync hardware - QSV reaches it through
oneVPL, VAAPI through libva, both via the `iHD` driver. There is nothing extra
to unlock.

`obs/Dockerfile` builds with `-DENABLE_QSV11=OFF` on purpose. It was tried on
this box: OBS logs `>>> app not on intel GPU`, falls back to the legacy MSDK
path because the frames are in system memory, and rejects the configuration
with `MFX_ERR_UNSUPPORTED`. The stream simply fails to start. VAAPI takes the
same frames without complaining, which is the whole difference.

---

## 5. Docker and deploy

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exec su -l $USER          # pick up the new group without a full logout
```

Point your domain's **A record** at the server IP first - Caddy needs to answer
the ACME challenge on port 80 at first start, and `DOMAIN` is now required (an
empty value stops Caddy from starting at all).

If the domain is on Cloudflare: **grey cloud / DNS-only**. It's the same
hostname your phone publishes SRT/SRTLA to, and Cloudflare's proxy doesn't
carry UDP.

```bash
git clone <your repo url> exnfachstreamen && cd exnfachstreamen
cp .env.example .env
nano .env
```

Filling in `.env`:

```bash
openssl rand -hex 16      # once each for PUBLISH_KEY, RESTREAM_KEY, OBS_WS_PASSWORD
docker run --rm caddy caddy hash-password --plaintext 'YOUR_PASSWORD'
```

Escape every `$` in that bcrypt hash as `$$` before pasting it into `.env` -
compose interpolates the value and silently eats bare `$`, which corrupts a
bcrypt hash beyond repair.

**Do not verify this with `docker compose config`.** That command re-escapes
`$` in its own output so the output stays reusable as a compose file, so a
*correct* 60-character hash shows up there as 63 characters. Following the
length check sends you off to "fix" a hash that was fine. Ask the container
what it actually receives instead:

```bash
docker compose exec dashboard sh -c 'printf "%s" "$DASH_PASS_HASH" | wc -c'
```

Expect exactly 60.

Then build and start. All three compose files, in this order:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.prod.yml \
               -f docker-compose.igpu.yml build     # see the note below
docker compose -f docker-compose.yml \
               -f docker-compose.prod.yml \
               -f docker-compose.igpu.yml up -d
docker compose logs -f watchdog
```

The build compiles OBS from source and downloads CEF, a ~300 MB Chromium
bundle, for browser sources. Budget 10-25 minutes for a cold build; on this
i7-7700 the OBS compile itself takes about five. A rebuild that only touches
the runtime stage is under a minute.

Drop `-f docker-compose.igpu.yml` if §4 didn't produce a `/dev/dri/renderD128`
- with the overlay and no device, the obs container refuses to start.

That command is long enough to get wrong at 3am. Pin it once:

```bash
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.igpu.yml' >> .env
```

After that plain `docker compose up -d` picks up all three by itself.

---

## 6. First-run checklist

1. `https://<your-domain>/` → log in. A real Let's Encrypt certificate, no
   browser warning - if you get one, Caddy didn't complete ACME; check
   `docker compose logs caddy` and that port 80 is actually reachable.
2. Add a Twitch channel (Creator Dashboard → Settings → Stream → primary key).
3. Upload a BRB video or image.
4. Point your phone at the URLs in the "Connect your encoder" card
   ([phone-setup.md](phone-setup.md)) and start it. Click a URL to copy it -
   that copies the real address even while the key is masked, so you can paste
   it without putting the key on a screen you may be sharing. The **Ingest**
   tile at the top goes green and **SRTLA links** shows 2 if bonding is on.
5. **Start stream** → confirm the Twitch channel actually goes live. Do not
   trust the panel alone here; see §4d for the way this fails silently.
6. Test the failsafe: airplane mode on the phone. BRB within ~2 s, back to
   live a few seconds after reconnect, stream and VOD unbroken. While it is
   degraded the top of the dashboard reads **ON AIR / BRB** in amber rather
   than green - that is the state worth recognising, because OBS is happily
   streaming while your viewers see the BRB screen.
7. Test the **Fix A/V** button while live - a second of black, then it
   re-latches. That's the button for audio drift or crackling when the ingest
   never actually dropped.

---

## 7. Scenes, failover and alerts

Everything in this section is driven from the dashboard, not from compose files.

### 7a. More than one live scene

`LIVE` is not the only scene that can be on air. Add others from the dashboard
(Scene card → *Live scenes*) for different layouts: a second camera, a
picture-in-picture, a scene with an overlay on top.

Adding one creates the scene in OBS and wires the ingest into it. Arrange the
rest in OBS itself; the dashboard only guarantees that the scene exists, shows
the stream, and takes part in failover.

Two properties worth knowing:

- **Every live scene shares one ingest source**, referenced rather than copied.
  A second `ffmpeg_source` would open a second SRT session against `sls`, which
  serves one player per stream id, so a copy would simply fail to connect. It
  also means *Fix A/V* affects all of them at once.
- **The watchdog returns to the scene you were on.** Lose the connection while
  on a PiP layout and you go to BRB, then back to that same layout - not to the
  default `LIVE`.

Scenes that are *not* registered as live are left alone entirely. A
"starting soon" screen stays up; the watchdog stands down while one is on
program rather than yanking you to BRB. Removing a scene from the live list is
not destructive: it stops taking part in failover, and the scene and its layout
stay in OBS.

### 7b. Pausing automatic switching

The Scene card has an **Auto-BRB** toggle. Building a scene is close to
impossible without it: whenever the phone is not publishing, the ingest counts
as down, and the watchdog drags the program scene back to BRB every couple of
seconds - so the scene you are trying to arrange keeps vanishing under the
mouse.

Pausing is persisted, so a dashboard restart will not silently resume switching
half way through setting something up. The hazard runs the other way: a pause
that survives into a live stream means there is no failsafe at all. That is why
the dashboard shows it as a warning rather than a quiet checkbox, and why the
watchdog logs both directions (`failover PAUSED from dashboard` /
`failover resumed`) - so you can tell afterwards whether a failsafe was even
armed during a dropout.

Turn it back on before you go live.

### 7c. Alerts (StreamElements, Streamlabs, ...)

The OBS build includes CEF, so **browser sources work**. Point one at your
existing hosted overlay URL - there is nothing to configure on the server:

1. OBS → Sources → **+** → Browser
2. URL: your overlay URL from StreamElements, Streamlabs or similar
3. Width 1920, Height 1080 (match your canvas from §8)
4. Add it to each live scene you want alerts on

Sound comes with it. If the alert audio does not reach the stream, tick
**Control audio via OBS** on the source so it appears in the mixer.

Positioning is done in the overlay editor of whatever service hosts it, since
these overlays are full-canvas by design. Nothing about alerts is stored on
this server.

CEF is amd64-only in `obs/Dockerfile`, gated the same way the QSV plumbing is;
an arm64 build keeps the browserless OBS it always had.

---

## 8. Tuning for this box

**You do not need the iGPU for 720p30.** The only encode in the whole stack is
OBS's single output - the RTMP fan-out to Twitch is `-c copy`, no transcoding
([`ingest/conf/push-to-rtmp.sh`](../ingest/conf/push-to-rtmp.sh)), and the
RTMP→SRT bridge is `-c copy` too. One 720p30 x264 `veryfast` stream is a small
fraction of a 7700. The iGPU buys headroom, not feasibility.

**The bigger CPU consumer is compositing, not encoding.** OBS renders through
llvmpipe (`LIBGL_ALWAYS_SOFTWARE=1`), and `docker-compose.igpu.yml`
deliberately does not change that - it hands over the encoder only. Cost scales
with canvas pixels, so going 1080p roughly doubles it.

**Resolution lives in two files and they have to agree.** If they drift, either
the encoder downscales every frame for nothing, or the watchdog stretches
sources past the edges of the canvas:

- `obs/config/profile-basic.ini` → `BaseCX/BaseCY` and `OutputCX/OutputCY`
- `watchdog/watchdog.py` → `CANVAS_W, CANVAS_H`

`install.sh` asks for the resolution and writes both, which is the reliable way
to do it. By hand, change both and raise `VBitrate` to ~6000 for 1080p. `profile-basic.ini` is only
seeded on first run, so an existing install needs the change made in the OBS
UI instead. Resetting the seed means dropping just that one volume, which
also wipes your scenes:

```bash
docker compose rm -sf obs
docker volume rm exnfachstreamen_obs_config  # check the real name: docker volume ls
docker compose up -d obs
```

**Watch it under load** with `docker stats` and `uptime`. On 8 threads you have
real room; load average creeping past ~6 is where to start worrying, not the
~2.0 the Oracle guide warns about.

**Traffic** is unmetered on a Hetzner dedicated box, so the Oracle egress
arithmetic is irrelevant. 1 Gbit/s is far more than a handful of Twitch
outputs will ever use.

**IPv6**: Hetzner hands you a /64, Docker doesn't use it by default, and
`push-to-rtmp.sh` already resolves Twitch to IPv4 explicitly to sidestep the
"AAAA record but no route" failure. Nothing to do.

---

## 9. When something's wrong

```bash
docker compose ps                       # who's actually up
docker compose logs -f ingest           # sls / srtla_rec / nginx
docker compose logs -f watchdog         # scene failover decisions
docker compose exec ingest supervisorctl status
docker compose exec ingest \
    curl -s localhost:8080/stats/<PLAY_KEY> # sls sees the publisher?
docker compose exec obs vainfo          # GPU still there?
```

Three failure modes worth knowing in advance:

- **`obs` restarting on loop with exit 139 and no OBS log at all.** Not OBS
  crashing. `docker compose stop obs` kills the X server without letting it
  clean up, and `docker compose up -d obs` restarts the *same* container with
  its writable layer - `/tmp` included. The leftover `/tmp/.X99-lock` makes the
  X server refuse to start, OBS finds no display and Qt aborts before it can
  open a log. The only trace is on container stdout:

  ```bash
  docker compose logs obs | tail -20    # "Server is already active for display 99"
  ```

  `obs/entrypoint.sh` clears the lock on start, so this should not happen any
  more. If it ever does, `docker compose up -d --force-recreate obs` gives the
  container a fresh writable layer. Note the knock-on: while `obs` is down it
  drops out of Docker's DNS, so Caddy answers **502** for the dashboard and
  `/obs/` - the 502 is a symptom, not a separate problem.

- **RTMP publishes get silently rejected** after a
  `docker compose up --force-recreate`. nginx resolves the `dashboard`
  hostname for its `on_publish` callback once, at config load, and keeps the
  old container IP. Nothing looks broken. `docker compose restart ingest`
  clears it.
- **Twitch connected but never live** → keyframe interval, see §4d.
