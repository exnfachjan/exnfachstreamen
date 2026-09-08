# exnfachstreamen

IRL streaming stack: bonded SRTLA ingest, headless OBS with BRB
failover, and multi-destination restreaming. Merged from IRL Toolkit and
streamserver (see Credits below).

A self-hosted "never drop your stream" rig: SRT / SRTLA / RTMP ingest under
**one stream key**, a headless OBS that fails over to a BRB scene the instant
your phone's connection dies, and simultaneous restreaming to as many Twitch
channels as you want.

This is a merge of two prior projects:

- **IRL Toolkit** - OBS + watchdog failover + dashboard + Caddy.
  Its own ingest (MediaMTX) is replaced here.
- **streamserver** - the SRT/SRTLA/RTMP ingest stack
  (`sls` + `srtla_rec` + nginx-rtmp), trimmed down (its Django management
  panel is gone; `sls` already has its own stream-id database and REST API,
  the panel was only a UI wrapper around it).

> The `Quellen/` entries in git history are gitlinks to two nested
> repositories that were never pushed, and there is no `.gitmodules` naming
> where to fetch them from. A clone gets two empty directories. The merged
> result is what this repository contains; the originals live in their own
> upstream projects.

## Install

```bash
git clone https://github.com/exnfachjan/exnfachstreamen.git
cd exnfachstreamen
sudo ./install.sh
```

The installer detects the host, fixes what that host gets wrong, installs
Docker, generates `.env`, checks DNS before Caddy ever contacts Let's Encrypt,
and builds. It is safe to re-run: every step checks the current state first, so
an interrupted run is resumed by running it again.

| Profile | For |
|---|---|
| `hetzner` | Hetzner dedicated. Removes `nomodeset`, lifts the i915/drm blacklist, verifies the render node and VA-API. Detected automatically. |
| `generic` | Anything else. Touches no boot config; reports what GPU it finds and picks the compose overlay accordingly. |

Force one with `--profile hetzner`. `--yes` accepts every default, `--skip-build`
configures without building.

On Hetzner the GPU fix needs a reboot. The installer stops there, says so, and
continues from that point when you run it again.

What it does **not** carry: `.env` and `data/` are machine-specific and stay out
of git. A second server gets fresh secrets rather than copies.


## Architecture

```
Phone / encoder (SRTLA, SRT, or RTMP - one key works for all three)
      │
      ▼
┌─────────────────────── ingest ───────────────────────┐
│  srtla_rec :5000 ──┐                                  │
│  sls (SRT)  :4001 ─┼─► sls core (playback :4000,      │
│  nginx-rtmp :1935 ─┘        stats/API :8080)          │
│                                                        │
│  nginx "restream" app ──push──► Twitch channel 1, 2, …│
└────────────────────────────────────────────────────────┘
      │  srt://ingest:4000?streamid=<PLAY_KEY>
      ▼
  OBS Studio (headless ARM64, Xvfb + noVNC, obs-websocket)
      │  LIVE ⇄ BRB, pushes to ingest's "restream" app
      ▼
  watchdog   - polls sls's stats API, drives OBS scene failover
  dashboard  - connect-URLs, Twitch channel list, BRB upload, status
  caddy      - TLS + auth in front of dashboard and noVNC
```

One publish key (`PUBLISH_KEY`) works for SRTLA, SRT and RTMP alike - that
was streamserver's flagship feature and it's preserved here. OBS never talks
to Twitch directly: it streams once into the ingest container's internal
`restream` RTMP app, which fans out to every Twitch channel configured in the
dashboard - no transcoding, no OBS plugin.

The fan-out is one `exec_push ffmpeg ... -c copy` process per channel, **not**
nginx-rtmp's native `push` directive. `push` has a crash bug that segfaults the
whole worker - taking every connection on it down with it - when a target
resolves to an unreachable IPv6 address, which Twitch's hostnames do on plenty
of networks. See the comment in `ingest/conf/nginx.conf`.

## Quick start (manual)

`./install.sh` above does all of this and checks more of it. This is the path
for when you would rather drive each step yourself.

1. `cp .env.example .env` and fill in every value (see the comments in the file).
2. `docker compose build`. OBS compiles from source and CEF, a ~300 MB Chromium
   bundle for browser sources, is downloaded. Budget 10-25 minutes cold; on a
   4-core/8-thread Intel box the OBS compile itself is about five.
3. `docker compose up -d`

On the real (bare-metal Linux) server, add `-f docker-compose.prod.yml` to
both commands above - it wires up `/dev/uinput` for Sunshine/Moonlight's
virtual mouse/keyboard input, a device that doesn't exist under Docker
Desktop, which is why the base file leaves it out. On an Intel host with a
working iGPU, add `-f docker-compose.igpu.yml` as well to hand OBS the render
node for hardware encoding.
4. Open `https://<your-domain>/`, log in, add your Twitch channel(s), upload a
   BRB video/image. (`DOMAIN` in `.env` is what Caddy serves on and gets the
   certificate for - see `docs/setup.md` if you don't have a domain yet.)
5. Point your phone app at the URLs shown in the dashboard's "Connect your
   encoder" card (see `docs/phone-setup.md`).
6. Hit **Start** in the dashboard.

Full server setup, pick the one matching your hardware:

- **Hetzner dedicated (Intel/amd64)** - `docs/setup-hetzner.md`. Covers
  installimage, the Docker-bypasses-ufw trap, and the Intel iGPU.
- **Oracle Cloud Always Free (Ampere/arm64)** - `docs/setup.md`. Covers the
  two-layer Oracle firewall and the sizing limits of a 2-OCPU box.

## Layout

| Path | Purpose |
|---|---|
| `install.sh`, `install/` | Installer and its host profiles (`hetzner`, `generic`) |
| `docker-compose.yml` | All services |
| `ingest/` | `sls` + `srtla_rec` + nginx-rtmp, single stream key, Twitch fan-out |
| `obs/` | OBS Studio built from source (amd64 and arm64) + headless entrypoint. CEF for browser sources on amd64 |
| `watchdog/` | BRB failover daemon. Polls `ingest`'s sls stats, drives scene switching across several live scenes |
| `dashboard/` | FastAPI control panel: stream and scene control, live scenes, Auto-BRB switch, connection info, Twitch destinations, BRB media |
| `caddy/` | Reverse proxy / TLS / auth (unchanged) |
| `docs/` | Server (Hetzner/amd64 and Oracle/arm64) + phone setup guides |

## What didn't make it in yet

- **Extra ingest paths** (IRL_Toolkit's "second camera feed" feature): needs
  the dashboard to hold `sls`'s admin API key to register additional
  publisher/player pairs at runtime. Deferred - the single Main Ingest covers
  the primary IRL use case.
- **Live ingest preview** in the dashboard: used to be MediaMTX's low-latency
  HLS. `sls` can do HLS too (`record_hls` in `ingest/conf/sls.conf`) but it
  isn't wired up to the dashboard yet.

## License

GPL-3.0-or-later - the image ships GPL-3.0 binaries (`sls`, `srtla_rec`, and
OBS Studio itself). See `LICENSE` and `ingest/THIRD-PARTY.md`.

## State of testing

Built and run on a Hetzner dedicated i7-7700 (Ubuntu 24.04, amd64). Verified
there: the image build, Caddy getting a real certificate over ACME, SRTLA
ingest with two bonded links from a phone, the watchdog's BRB failover and its
return to the previous live scene, hardware VA-API encoding, browser sources,
the live-scene and Auto-BRB controls, and restreaming to two Twitch channels at
once. `sls`'s `/stats` response shape - which the watchdog's `healthy()` reads -
was confirmed against the real build.

Not yet verified:

- **`install.sh` on a fresh server.** Its parts were exercised individually on
  an already-configured machine; a run from a clean OS install has not happened.
- **A sustained IRL field test.** One attempt dropped repeatedly, which traced
  to the phone's uplink rather than the stack: bonding a home wifi with mobile
  means walking out of range halves the available bitrate, and a fixed encoder
  bitrate collapses at that moment. Set the phone to adaptive and size it for
  the weakest single link.
- **arm64.** The Oracle Ampere path in `docs/setup.md` has not been re-checked
  since CEF and the QSV plumbing were added; both are gated to amd64 and should
  be inert there, but nobody has built it.
