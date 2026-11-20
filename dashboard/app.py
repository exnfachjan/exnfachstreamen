"""Control-panel backend: connection info, Twitch destinations, BRB media,
scenes, status.

Talks to OBS over obs-websocket and to the `ingest` container's sls stats API
(plain HTTP, unauthenticated stats endpoint - see ingest/conf/sls.conf).
Persists config in /data/config.json (volume).

Fork note (merge of IRL_Toolkit + streamserver): this used to talk to
MediaMTX and offer a Django-panel-style "add extra ingest path" feature.
MediaMTX is gone - ingest is now the streamserver-derived sls/srtla_rec/
nginx-rtmp stack, single publisher/player key pair (PUBLISH_KEY/PLAY_KEY,
see .env.example), bootstrapped once by ingest/conf/entrypoint.sh. The RTMP
publish-auth callback that Django's SLSPanel used to serve now lives here
(/rtmp/auth/publish, /rtmp/auth/restream).

Multi-destination Twitch output replaces OBS's own single rtmp_custom output:
OBS streams once to the ingest container's internal nginx "restream" RTMP
app, which fans out to every configured Twitch channel via one ffmpeg
pull-and-push process per channel (-c copy, no re-encoding) - not nginx-rtmp's
native `push` directive, which has a known crash bug (segfaults the whole
worker, taking every connection on it down, if a push target resolves to an
unreachable IPv6 address - see write_restream_targets). No OBS plugin needed
either way. This module owns the channel list and rewrites the shared
restream.d/targets.conf file that nginx includes (see ingest/conf/nginx.conf);
ingest/conf/reload-watcher.sh picks up changes and reloads nginx.

The multi-path "extra ingest" feature (secondary camera feeds etc.) from
IRL_Toolkit is not ported yet - it would need the dashboard to hold sls's
admin API key to register extra publisher/player pairs at runtime. Left as a
follow-up; the single Main Ingest covers the primary use case.
"""

import json
import os
import re
import itertools
import random
import secrets
import shlex
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import bcrypt
import requests
import obsws_python as obsws
from fastapi import Cookie, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OBS_HOST = os.environ.get("OBS_HOST", "obs")
OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD = os.environ["OBS_WS_PASSWORD"]
WATCHDOG_URL = os.environ.get("WATCHDOG_URL", "http://watchdog:8081")
INGEST_STATS_URL = os.environ.get("INGEST_STATS_URL", "http://ingest:8080")
INGEST_RTMP_HOST = os.environ.get("INGEST_RTMP_HOST", "ingest")

# The one key pair every protocol (SRT, SRTLA, RTMP) publishes under, and the
# internal secret OBS's own output uses to reach the restream app. All three
# are plain env vars shared with the `ingest` container via .env - no volume
# or API-key exchange needed since there's exactly one of each.
PUBLISH_KEY = os.environ["PUBLISH_KEY"]
PLAY_KEY = os.environ["PLAY_KEY"]
RESTREAM_KEY = os.environ["RESTREAM_KEY"]

# Public-facing address shown in the connection-info card (your domain / IP -
# whatever phones and encoders actually reach this server on).
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
SRTLA_PORT = os.environ.get("SRTLA_PORT", "5000")
SRT_PORT = os.environ.get("SRT_PORT", "4001")
RTMP_PORT = os.environ.get("RTMP_PORT", "1935")

RESTREAM_TARGETS_FILE = Path(
    os.environ.get("RESTREAM_TARGETS_FILE", "/restream.d/targets.conf")
)
TWITCH_RTMP_HOST = "live.twitch.tv"
TWITCH_RTMP_APP = "app"
TWITCH_RTMP_BASE = f"rtmp://{TWITCH_RTMP_HOST}/{TWITCH_RTMP_APP}"

CONFIG_FILE = Path("/data/config.json")
MEDIA_DIR = Path("/media/brb")  # same path inside the OBS container
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# A BRB loop is a short clip or a still. The cap exists because a full disk
# takes the whole stack down - sls, nginx and OBS all stop being able to write -
# which is a bad way to discover that someone uploaded a feature film.
MAX_BRB_BYTES = int(os.environ.get("MAX_BRB_BYTES", 512 * 1024 * 1024))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".flv"}

SCENE_BRB = "BRB"
SOURCE_BRB_MEDIA = "BRB Media"
SOURCE_BRB_IMAGE = "BRB Image"

# Optional BRB playlist: a YouTube playlist and/or a queue of the channel's
# recent Twitch clips, played by a browser source on top of the BRB media.
# The page itself is static/brb-player.html, served from here; OBS loads it
# over the compose network, so it never goes through Caddy's login.
SOURCE_BRB_PLAYER = "BRB Player"
PLAYER_URL = "http://dashboard:8000/brb/player"
PLAYER_MODES = ("off", "youtube", "clips", "both")
CLIP_RANGES = ("24hr", "7d", "30d", "6m", "1y", "all")
# Twitch's listing filters 24hr/7d/30d itself; 6m/1y are listed all-time and
# cut here. Applied to every range anyway, as a check on the listing.
CLIP_RANGE_DAYS = {"24hr": 1, "7d": 7, "30d": 30, "6m": 183, "1y": 365}
# top = most viewed first; random = any clip in range, a few swapped per refresh.
CLIP_PICKS = ("top", "random")
CLIP_ROTATE = 5
CLIP_MAX_LENS = (0, 15, 20, 30, 45, 60)   # 0 = no limit
CLIPPER_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
CLIPS_DIR = MEDIA_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_INDEX = CLIPS_DIR / "index.json"
# Clips are 5-60 s each; this caps the download at a few hundred MB.
MAX_CLIPS = int(os.environ.get("BRB_MAX_CLIPS", "20"))
CLIPS_REFRESH_SEC = int(os.environ.get("BRB_CLIPS_REFRESH_SEC", "1800"))
YT_LIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")

# Live scenes: several layouts (wide shot, PiP, chat overlay, ...) that all
# count as "on air". This module owns the list; the watchdog reads it
# read-only from the same config file and treats every entry as live, so a
# BRB detour returns to whichever one was on screen. Keep the name in sync
# with watchdog/watchdog.py's SCENE_LIVE_DEFAULT.
SCENE_LIVE_DEFAULT = "LIVE"

# The LIVE scene's SRT input. Created by the watchdog on boot, not here - the
# name has to stay in sync with watchdog/watchdog.py's SOURCE_INGEST.
SOURCE_INGEST = "Ingest"

# Twitch stream keys are live_<digits>_<alphanumerics>. Restricting the charset
# is the real protection for write_restream_targets(): that function writes into
# an *nginx* config, and shlex.quote() quotes for a *shell*. The two disagree -
# shlex renders an embedded apostrophe as the '"'"' idiom, which nginx reads as
# three separate tokens rather than one escaped quote. Since the directive being
# written is `exec_push <command>`, a key that breaks out of the quoting turns
# into command execution. Rejecting the characters outright is simpler and more
# durable than trying to quote correctly for two languages at once.
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
# Labels are display-only, but they are persisted and rendered, so keep them
# short and free of control characters.
LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS_HASH = os.environ.get("DASH_PASS_HASH", "")

SESSION_TTL = 7 * 86400
_sessions: dict[str, float] = {}  # token -> expiry (in-memory; re-login on restart)

app = FastAPI(title="exnfachstreamen dashboard")


# ---------- config ----------

_cfg_lock = threading.Lock()


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    cfg.setdefault("twitch_channels", [])  # [{id, name, key}]
    cfg.setdefault("brb_file", None)
    player = cfg.get("brb_player") if isinstance(cfg.get("brb_player"), dict) else {}
    cfg["brb_player"] = {
        "mode": player.get("mode") if player.get("mode") in PLAYER_MODES else "off",
        "youtube_list": player.get("youtube_list") or "",
        "clips_channel": player.get("clips_channel") or "",
        "clips_range": player.get("clips_range") if player.get("clips_range") in CLIP_RANGES else "7d",
        "clips_pick": player.get("clips_pick") if player.get("clips_pick") in CLIP_PICKS else "top",
        "clips_max_len": player.get("clips_max_len") if player.get("clips_max_len") in CLIP_MAX_LENS else 0,
        "clips_len_mode": "cut" if player.get("clips_len_mode") == "cut" else "skip",
        "clips_exclude": [w for w in player.get("clips_exclude") or [] if isinstance(w, str)][:30],
        "clips_creators": [w for w in player.get("clips_creators") or [] if isinstance(w, str)][:30],
        "overlay_info": player.get("overlay_info") is not False,
        "overlay_timer": bool(player.get("overlay_timer", False)),
        "shuffle": bool(player.get("shuffle", True)),
        "volume": max(0, min(100, int(player.get("volume", 60)))),
    }
    # The default live scene is not stored as removable data - it is the
    # floor the watchdog falls back to, so it is always present and always
    # first, whatever the file says.
    scenes = [x for x in cfg.get("live_scenes", [])
              if isinstance(x, str) and x and x != SCENE_BRB]
    if SCENE_LIVE_DEFAULT in scenes:
        scenes.remove(SCENE_LIVE_DEFAULT)
    cfg["live_scenes"] = [SCENE_LIVE_DEFAULT] + scenes
    # Only an explicit False pauses failover - see the same rule in
    # watchdog/watchdog.py's load_watch_config(). Losing the failsafe to a
    # missing or malformed value would be far worse than ignoring the toggle.
    cfg["failover_enabled"] = cfg.get("failover_enabled") is not False
    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def write_restream_targets(channels):
    """One push-to-rtmp.sh (ffmpeg pull-back-and-push, see ingest/conf/) per
    Twitch channel, not nginx-rtmp's native `push` directive. nginx-rtmp's
    push has a known crash bug: if the target host resolves to an IPv6
    address and that address is unreachable (true of Docker Desktop always -
    no outbound IPv6 route - and possible on a real network too), the worker
    process segfaults and takes down every connection it's handling,
    including the unrelated phone/RTMP ingest on the same nginx. A crashing
    ffmpeg instead only takes down that one channel's push. push-to-rtmp.sh
    additionally resolves the destination by IPv4 itself for the same
    underlying reason (ffmpeg has no happy-eyeballs fallback between address
    families either) - see that script for the full explanation.
    """
    RESTREAM_TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        "exec_push /usr/local/bin/push-to-rtmp.sh "
        f"$name {shlex.quote(TWITCH_RTMP_HOST)} {shlex.quote(TWITCH_RTMP_APP)} "
        f"{shlex.quote(c['key'])} >>/tmp/ffmpeg-restream-{c['id']}.log 2>&1;\n"
        for c in channels
    )
    RESTREAM_TARGETS_FILE.write_text(lines)


# ---------- OBS client (persistent, reconnect on failure) ----------

_obs_lock = threading.Lock()
_obs_cl = None


def obs_client():
    global _obs_cl
    with _obs_lock:
        if _obs_cl is None:
            try:
                _obs_cl = obsws.ReqClient(
                    host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5
                )
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"OBS unreachable: {e}")
        return _obs_cl


def obs_reset():
    global _obs_cl
    with _obs_lock:
        try:
            if _obs_cl:
                _obs_cl.disconnect()
        except Exception:
            pass
        _obs_cl = None


def with_obs(fn):
    """Run fn(client); on connection failure reconnect once and retry."""
    try:
        return fn(obs_client())
    except HTTPException:
        raise
    except Exception:
        obs_reset()
        try:
            return fn(obs_client())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"OBS call failed: {e}")


def ensure_obs_restream_target():
    """OBS always streams to our own internal restream app, never straight to
    Twitch - the actual Twitch fan-out happens in nginx (see module docstring).
    Idempotent; retried in the background since OBS may not be up yet.
    """
    while True:
        try:
            with_obs(lambda cl: cl.set_stream_service_settings(
                "rtmp_custom",
                {
                    "server": f"rtmp://{INGEST_RTMP_HOST}:1935/restream",
                    "key": RESTREAM_KEY,
                    "use_auth": False,
                },
            ))
            return
        except HTTPException:
            time.sleep(3)


threading.Thread(target=ensure_obs_restream_target, daemon=True).start()


def sync_restream_targets_on_boot():
    write_restream_targets(load_config()["twitch_channels"])


def normalise_config_on_boot():
    """Write back the config with every default filled in.

    load_config() adds missing keys in memory, but the watchdog reads the
    *file*, not this process. Persisting once at boot means live_scenes is
    actually there for it after an upgrade from a config written before that
    key existed, instead of only appearing the first time something else
    happens to save.
    """
    with _cfg_lock:
        save_config(load_config())


sync_restream_targets_on_boot()
normalise_config_on_boot()


# ---------- ingest stats ----------

def ingest_stats():
    try:
        r = requests.get(f"{INGEST_STATS_URL}/stats/{PLAY_KEY}", timeout=2)
        if r.status_code != 200:
            return {"online": False, "bitrate_kbps": 0}
        info = r.json()
        publisher = info.get("publisher") if info.get("status") == "ok" else None
        if not isinstance(publisher, dict):
            return {"online": False, "bitrate_kbps": 0}
        return {
            "online": True,
            "bitrate_kbps": int(publisher.get("bitrate") or 0),
            "rtt_ms": publisher.get("rtt"),
            "dropped_pkts": publisher.get("dropped_pkts"),
            "peers": len(publisher.get("peers") or []),  # >1 = SRTLA bonded links
        }
    except requests.RequestException:
        return {"online": False, "bitrate_kbps": 0}


# ---------- auth ----------

def session_valid(token):
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


class Login(BaseModel):
    username: str
    password: str


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


@app.post("/api/login")
def login(body: Login, response: Response):
    # Compare as bytes: secrets.compare_digest() raises TypeError on strings
    # holding non-ASCII, which would turn a wrong username into an unhandled
    # 500 instead of a 401 - and a response that differs by more than the
    # status code is exactly what username enumeration needs.
    ok = secrets.compare_digest(
        body.username.encode(), DASH_USER.encode()
    ) and bcrypt.checkpw(body.password.encode(), DASH_PASS_HASH.encode())
    if not ok:
        time.sleep(0.5)  # blunt brute-force damper
        raise HTTPException(status_code=401, detail="wrong username or password")
    # Expired entries are otherwise only dropped when that exact token is
    # presented again, so tokens from browsers that never come back accumulate
    # for the life of the process. Cheap to sweep here: one login, one pass.
    now = time.time()
    for stale in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(stale, None)

    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_TTL
    response.set_cookie(
        "session", token, max_age=SESSION_TTL,
        httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        _sessions.pop(session, None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/auth/verify")
def auth_verify(session: str | None = Cookie(default=None)):
    """Caddy forward_auth hits this for every protected request."""
    if session and session_valid(session):
        return Response(status_code=204)
    return RedirectResponse("/login", status_code=302)


# ---------- RTMP publish-auth callbacks (nginx on_publish, see ingest/conf/nginx.conf) ----------

@app.post("/rtmp/auth/publish")
async def rtmp_auth_publish(request: Request):
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if name and secrets.compare_digest(name, PUBLISH_KEY):
        return Response("ok", status_code=200)
    return Response("forbidden", status_code=403)


@app.post("/rtmp/auth/restream")
async def rtmp_auth_restream(request: Request):
    """Only OBS's own output should ever hit this - not a real viewer/encoder
    endpoint. Guards the restream app in case 1935 is reachable from outside.
    """
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if name and secrets.compare_digest(name, RESTREAM_KEY):
        return Response("ok", status_code=200)
    return Response("forbidden", status_code=403)


# ---------- status ----------

@app.get("/api/status")
def status():
    out = {"watchdog": None, "obs": None, "scenes": None, "ingest": None,
           "twitch_channels": [], "brb_file": None, "live_scenes": [],
           "failover_enabled": True}
    try:
        out["watchdog"] = requests.get(WATCHDOG_URL, timeout=2).json()
    except requests.RequestException:
        pass

    def obs_part(cl):
        s = cl.get_stream_status()
        sc = cl.get_scene_list()
        try:
            muted = cl.get_input_mute(SOURCE_INGEST).input_muted
        except Exception:
            # Watchdog hasn't bootstrapped the source yet on a cold start.
            muted = None
        return (
            {
                "streaming": s.output_active,
                "duration_ms": s.output_duration,
                "dropped_frames": s.output_skipped_frames,
                "total_frames": s.output_total_frames,
            },
            {
                "current": sc.current_program_scene_name,
                "all": [x["sceneName"] for x in reversed(sc.scenes)],
            },
            muted,
        )

    ingest_muted = None
    try:
        out["obs"], out["scenes"], ingest_muted = with_obs(obs_part)
    except HTTPException:
        pass

    out["ingest"] = ingest_stats()
    # None = unknown (OBS unreachable, or the source doesn't exist yet); the
    # UI disables the mute button rather than showing a state it can't know.
    out["ingest"]["muted"] = ingest_muted

    cfg = load_config()
    out["twitch_channels"] = [
        {"id": c["id"], "name": c["name"]} for c in cfg["twitch_channels"]
    ]
    out["brb_file"] = cfg.get("brb_file")
    out["brb_player"] = cfg["brb_player"]
    out["live_scenes"] = cfg["live_scenes"]
    out["failover_enabled"] = cfg["failover_enabled"]
    return out


@app.get("/api/connection-info")
def connection_info():
    host = PUBLIC_HOST or "<server-ip-or-domain>"
    return {
        "srtla": f"srtla://{host}:{SRTLA_PORT}?streamid={PUBLISH_KEY}",
        "srt": f"srt://{host}:{SRT_PORT}?streamid={PUBLISH_KEY}",
        # Combined server+app+key in one URL (rtmp://host:port/app/key) -
        # what most encoders/apps expect in a single "server" field. Some
        # (OBS included) split this into a separate "Server" + "Stream key"
        # pair instead; server is everything up to /live, key is PUBLISH_KEY.
        "rtmp": f"rtmp://{host}:{RTMP_PORT}/live/{PUBLISH_KEY}",
        "rtmp_server": f"rtmp://{host}:{RTMP_PORT}/live",
        "rtmp_key": PUBLISH_KEY,
    }


# ---------- stream control ----------

@app.post("/api/stream/start")
def start_stream():
    with_obs(lambda cl: cl.start_stream())
    return {"ok": True}


@app.post("/api/stream/stop")
def stop_stream():
    with_obs(lambda cl: cl.stop_stream())
    return {"ok": True}


# ---------- scenes ----------

class SceneReq(BaseModel):
    name: str


@app.post("/api/scene")
def set_scene(body: SceneReq):
    with_obs(lambda cl: cl.set_current_program_scene(body.name))
    return {"ok": True}


# ---------- failover switch ----------

class FailoverReq(BaseModel):
    enabled: bool


@app.post("/api/failover")
def set_failover(body: FailoverReq):
    """Pause or resume automatic BRB scene switching.

    Needed for any scene work at all: while the ingest is down - which it is
    whenever the phone isn't publishing - the watchdog pulls the program
    scene back to BRB every couple of seconds, so a scene you are trying to
    build (placing an alert overlay, arranging a PiP layout) keeps vanishing
    under the mouse.

    Persisted rather than held in memory: the watchdog reads the same file,
    and a dashboard restart must not silently resume switching mid-setup.
    The reverse - a paused failsafe surviving into a live stream - is the
    real hazard here, which is why the dashboard shows it as a warning
    instead of a quiet checkbox.
    """
    with _cfg_lock:
        cfg = load_config()
        cfg["failover_enabled"] = body.enabled
        save_config(cfg)
    return {"ok": True, "failover_enabled": body.enabled}


# ---------- live scenes ----------

@app.get("/api/live-scenes")
def get_live_scenes():
    return {"live_scenes": load_config()["live_scenes"],
            "default": SCENE_LIVE_DEFAULT}


@app.post("/api/live-scenes")
def add_live_scene(body: SceneReq):
    """Register another scene as "on air" and wire the ingest into it.

    Creates the scene in OBS if it doesn't exist and references the existing
    Ingest source into it - one input shared by every live scene, never a
    copy. A second ffmpeg_source would open a second SRT session against sls,
    which serves one player per stream id, so the copy would simply fail to
    connect.

    Everything else about the layout (resizing the ingest, adding a camera,
    text, an overlay) is done in OBS itself; this only guarantees the scene
    exists, shows the stream, and takes part in BRB failover.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Scene name required")
    if not LABEL_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="scene name must be 1-64 characters and contain no control characters",
        )
    if name == SCENE_BRB:
        raise HTTPException(
            status_code=400,
            detail="BRB is the failsafe screen and can't also be a live scene",
        )
    cfg = load_config()
    if name in cfg["live_scenes"]:
        raise HTTPException(status_code=409, detail=f"{name} is already a live scene")

    def setup(cl):
        existing = {x["sceneName"] for x in cl.get_scene_list().scenes}
        if name not in existing:
            cl.create_scene(name)
        try:
            cl.get_scene_item_id(name, SOURCE_INGEST)
        except Exception:
            cl.create_scene_item(name, SOURCE_INGEST, True)

    with_obs(setup)

    with _cfg_lock:
        cfg = load_config()
        if name not in cfg["live_scenes"]:
            cfg["live_scenes"].append(name)
            save_config(cfg)
    return {"ok": True, "live_scenes": load_config()["live_scenes"]}


@app.delete("/api/live-scenes/{name}")
def delete_live_scene(name: str):
    """Stop treating a scene as live. The scene itself stays in OBS.

    Deliberately not destructive: a live scene usually holds a hand-built
    layout, and one click in a web UI is the wrong weight for throwing that
    away. Unregistering is reversible; deleting the scene in OBS is the
    user's call.
    """
    if name == SCENE_LIVE_DEFAULT:
        raise HTTPException(
            status_code=400,
            detail=f"{SCENE_LIVE_DEFAULT} is the fallback scene and can't be removed",
        )
    with _cfg_lock:
        cfg = load_config()
        if name not in cfg["live_scenes"]:
            raise HTTPException(status_code=404, detail=f"{name} is not a live scene")
        cfg["live_scenes"] = [x for x in cfg["live_scenes"] if x != name]
        save_config(cfg)
    return {"ok": True, "live_scenes": load_config()["live_scenes"]}


# ---------- ingest source ----------

@app.post("/api/ingest/fix")
def fix_ingest():
    """Restart the shared SRT ingest source.

    The one-button remedy for the stream being technically up but sounding or
    looking wrong - audio drifted out of sync, or crackling after a rough
    reconnect. Restarting the ffmpeg_source tears down and reopens its SRT
    session, dropping every buffer, so picture and sound restart together from
    a fresh keyframe.

    Aimed at exactly the failures the watchdog cannot see. Its health test is
    "publisher present AND bitrate > 0" (watchdog/watchdog.py), and both hold
    while audio is drifting, so it never intervenes. On its way back from BRB
    the watchdog runs this same action before switching scenes; this endpoint
    exposes it for the cases where nothing ever went down.

    Affects every live scene at once. There is one Ingest input, referenced
    from each live scene rather than copied into it (see add_live_scene), so
    there is exactly one SRT session to restart - which is also the only thing
    sls would serve, since it allows a single player per stream id.

    Costs one to two seconds of black wherever that source is on screen. The
    Twitch output is untouched: OBS keeps encoding and pushing to the restream
    app throughout, so the stream does not drop, Twitch does not reconnect and
    the VOD stays continuous.

    Does not help when the phone's uplink is the actual problem - it reopens
    the same struggling connection. That belongs to bitrate settings on the
    encoder, not here.
    """
    with_obs(lambda cl: cl.trigger_media_input_action(
        SOURCE_INGEST, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"))
    return {"ok": True}


@app.post("/api/ingest/mute")
def mute_ingest():
    """Toggle audio on the ingest source (kills phone-side noise, keeps video)."""

    def toggle(cl):
        cl.toggle_input_mute(SOURCE_INGEST)
        return cl.get_input_mute(SOURCE_INGEST).input_muted

    return {"ok": True, "muted": with_obs(toggle)}


# ---------- Twitch destinations ----------

class TwitchChannel(BaseModel):
    name: str
    key: str


@app.post("/api/twitch-channels")
def add_twitch_channel(body: TwitchChannel):
    name = body.name.strip()
    key = body.key.strip()
    if not name or not key:
        raise HTTPException(status_code=400, detail="name and key are required")
    if not LABEL_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="label must be 1-64 characters and contain no control characters",
        )
    if not KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="stream key may only contain letters, digits, '_' and '-'",
        )
    with _cfg_lock:
        cfg = load_config()
        cfg["twitch_channels"].append(
            {"id": secrets.token_hex(4), "name": name, "key": key}
        )
        save_config(cfg)
        write_restream_targets(cfg["twitch_channels"])
    return {"ok": True}


@app.delete("/api/twitch-channels/{channel_id}")
def delete_twitch_channel(channel_id: str):
    with _cfg_lock:
        cfg = load_config()
        before = len(cfg["twitch_channels"])
        cfg["twitch_channels"] = [
            c for c in cfg["twitch_channels"] if c["id"] != channel_id
        ]
        if len(cfg["twitch_channels"]) == before:
            raise HTTPException(status_code=404, detail="no such channel")
        save_config(cfg)
        write_restream_targets(cfg["twitch_channels"])
    return {"ok": True}


# ---------- BRB media ----------

@app.post("/api/brb")
async def upload_brb(file: UploadFile):
    ext = Path(file.filename or "brb").suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")
    dest = MEDIA_DIR / f"brb{ext}"
    # Copied in bounded chunks rather than with copyfileobj: the request body
    # is attacker-controlled in length, and the point is to stop before the
    # disk fills, not after.
    written = 0
    try:
        with dest.open("wb") as f:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_BRB_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"file larger than {MAX_BRB_BYTES // (1024*1024)} MB",
                    )
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    is_image = ext in IMAGE_EXTS

    def apply(cl):
        if is_image:
            cl.set_input_settings(SOURCE_BRB_IMAGE, {"file": str(dest)}, True)
        else:
            cl.set_input_settings(SOURCE_BRB_MEDIA, {
                "is_local_file": True, "local_file": str(dest), "looping": True,
            }, True)
        for src, enabled in ((SOURCE_BRB_IMAGE, is_image),
                             (SOURCE_BRB_MEDIA, not is_image)):
            item_id = cl.get_scene_item_id(SCENE_BRB, src).scene_item_id
            cl.set_scene_item_enabled(SCENE_BRB, item_id, enabled)

    with_obs(apply)
    with _cfg_lock:
        cfg = load_config()
        cfg["brb_file"] = dest.name
        save_config(cfg)
    return {"ok": True, "file": dest.name}


# ---------- BRB player (optional YouTube playlist / Twitch clip queue) ----------

_clips_lock = threading.Lock()
# Set when a refresh is requested while one is running (e.g. settings saved
# mid-download): the running one would finish with the OLD settings, so it
# re-runs once more right after instead of the request being dropped.
_clips_pending = threading.Event()
_clips_status = {"count": 0, "updated": None, "error": None, "busy": False, "progress": None}


def read_clip_index():
    try:
        return json.loads(CLIPS_INDEX.read_text())
    except (OSError, ValueError):
        return []


def transcode_clip(src, dest):
    """Twitch serves clips as HEVC only (every quality, since 2026), and the
    Chromium inside OBS's browser source has no software HEVC decoder - the
    clip plays as a black frame. So each clip is converted once to H.264.
    720p keeps it cheap: niced, one at a time, a 60 s clip takes seconds."""
    tmp = dest.with_suffix(".part.mp4")
    subprocess.run(
        ["nice", "-n", "15", "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-i", str(src), "-vf", "scale=-2:'min(720,ih)'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", str(tmp)],
        check=True, timeout=600,
    )
    tmp.replace(dest)


# Settings that decide WHICH clips are on disk; a change to any of them
# triggers a refresh. (Overlay, volume, shuffle only affect playback.)
CLIP_SELECTION_KEYS = ("clips_channel", "clips_range", "clips_pick", "clips_max_len",
                       "clips_len_mode", "clips_exclude", "clips_creators")


def clip_candidates(p, listing):
    """Filter the channel's clip listing by date, length and title words.
    The flat listing already carries title, duration, date and views, so
    this costs no request per clip."""
    cutoff = time.time() - CLIP_RANGE_DAYS[p["clips_range"]] * 86400 \
        if p["clips_range"] in CLIP_RANGE_DAYS else 0
    words = [w.casefold() for w in p["clips_exclude"]]
    out = []
    for r in listing:
        if not r or not r.get("url"):
            continue
        if (r.get("timestamp") or 0) < cutoff:
            continue
        if p["clips_max_len"] and p["clips_len_mode"] == "skip" \
                and (r.get("duration") or 0) > p["clips_max_len"]:
            continue
        title = (r.get("title") or "").casefold()
        if any(w in title for w in words):
            continue
        out.append(r)
    return out


def refresh_clips():
    """Fill /media/brb/clips with up to MAX_CLIPS clips of the channel.

    yt-dlp lists them through Twitch's public site API, so no Twitch app or
    token is needed. "top" takes the most viewed; "random" keeps most of what
    is already on disk and swaps CLIP_ROTATE for new random ones per refresh,
    so the BRB screen changes over the day without re-downloading everything.
    Clips already on disk are reused; the rest are deleted.
    """
    p = load_config()["brb_player"]
    if p["mode"] not in ("clips", "both") or not p["clips_channel"]:
        return
    if not _clips_lock.acquire(blocking=False):
        _clips_pending.set()   # picked up when the running refresh ends
        return
    _clips_pending.clear()
    _clips_status.update(busy=True, error=None, progress="listing clips")
    try:
        from yt_dlp import YoutubeDL  # heavy import, only when used
        rng = p["clips_range"] if p["clips_range"] in ("24hr", "7d", "30d") else "all"
        url = f"https://www.twitch.tv/{p['clips_channel']}/clips?filter=clips&range={rng}"
        quiet = {"quiet": True, "no_warnings": True, "noprogress": True}
        with YoutubeDL({**quiet, "extract_flat": True}) as ydl:
            listing = (ydl.extract_info(url, download=False) or {}).get("entries") or []
        cands = clip_candidates(p, list(listing))

        prev = {c.get("id"): c for c in read_clip_index()
                if c.get("id") and (CLIPS_DIR / c["file"]).exists()}
        if p["clips_pick"] == "top":
            ordered = sorted(cands, key=lambda r: r.get("view_count") or 0, reverse=True)
        else:
            have = [r for r in cands if r.get("id") in prev]
            new = [r for r in cands if r.get("id") not in prev]
            random.shuffle(have)
            random.shuffle(new)
            keep_n = max(0, MAX_CLIPS - CLIP_ROTATE)
            ordered = have[:keep_n] + new + have[keep_n:]

        creators = {c.casefold() for c in p["clips_creators"]}
        clips, tried = [], 0
        dl_opts = {**quiet, "format": "720/best[height<=720]/best",
                   "outtmpl": str(CLIPS_DIR / "%(id)s.src.%(ext)s")}
        with YoutubeDL(dl_opts) as ydl:
            for ref in ordered:
                if len(clips) >= MAX_CLIPS or tried >= MAX_CLIPS * 4:
                    break
                _clips_status["progress"] = f"clip {len(clips) + 1}/{MAX_CLIPS}"
                old = prev.get(ref.get("id"))
                if old:
                    if not creators or old.get("clipper", "").casefold() in creators:
                        clips.append(old)
                    continue
                tried += 1
                try:
                    info = ydl.extract_info(ref["url"], download=False)
                    clipper = info.get("uploader") or ""
                    if creators and clipper.casefold() not in creators:
                        continue
                    if not clip_candidates(p, [{**info, "url": ref["url"]}]):
                        continue   # the clip's own metadata fails a filter
                    final = CLIPS_DIR / f"{info['id']}.mp4"
                    if not final.exists():
                        info = ydl.extract_info(ref["url"], download=True)
                        src = Path(ydl.prepare_filename(info))
                        try:
                            transcode_clip(src, final)
                        finally:
                            src.unlink(missing_ok=True)
                    clips.append({
                        "id": info["id"], "file": final.name,
                        "title": info.get("title") or "",
                        "duration": info.get("duration") or 0,
                        "timestamp": info.get("timestamp") or 0,
                        "views": info.get("view_count") or 0,
                        "clipper": clipper,
                        "game": (info.get("categories") or [""])[0] or "",
                    })
                except Exception:
                    continue  # one broken clip must not cost the rest

        if not clips:
            # Nothing came back (filters too strict, Twitch hiccup, rate
            # limit): keep playing what is on disk rather than wiping it.
            _clips_status.update(candidates=len(cands),
                                 error="no clips match these settings - keeping the previous ones")
            return
        if p["clips_pick"] == "top":
            clips.sort(key=lambda c: c.get("views") or 0, reverse=True)
        keep = {c["file"] for c in clips} | {CLIPS_INDEX.name}
        for f in CLIPS_DIR.iterdir():
            if f.name not in keep:
                f.unlink(missing_ok=True)
        CLIPS_INDEX.write_text(json.dumps(clips))
        _clips_status.update(count=len(clips), updated=time.strftime("%H:%M"),
                             candidates=len(cands))
    except Exception as e:
        _clips_status["error"] = str(e)[:200]
    finally:
        _clips_status.update(busy=False, progress=None)
        _clips_lock.release()
    if _clips_pending.is_set():
        refresh_clips()


def clips_loop():
    _clips_status["count"] = len(read_clip_index())
    while True:
        refresh_clips()
        time.sleep(CLIPS_REFRESH_SEC)


threading.Thread(target=clips_loop, daemon=True).start()


def apply_brb_player(cl, player):
    """Create/update the browser source and show it only when a mode is on.

    The BRB media underneath stays visible as the fallback (the page is
    transparent while it has nothing to play) but is muted whenever the
    player is on, so the two never talk over each other.
    """
    if SCENE_BRB not in {s["sceneName"] for s in cl.get_scene_list().scenes}:
        raise RuntimeError("BRB scene not created yet")
    on = player["mode"] != "off"
    v = cl.get_video_settings()
    settings = {
        "url": PLAYER_URL, "width": v.base_width, "height": v.base_height,
        "reroute_audio": True,      # audio into OBS's mixer -> into the stream
        "shutdown": True,           # stops (and goes silent) outside BRB
        "restart_when_active": True,  # fresh page, fresh clip list, each BRB
        "css": "body { background-color: rgba(0,0,0,0); margin: 0; overflow: hidden; }",
    }
    names = {i["inputName"] for i in cl.get_input_list().inputs}
    if SOURCE_BRB_PLAYER not in names:
        # browser_source exists only where OBS was built with CEF (amd64).
        cl.create_input(SCENE_BRB, SOURCE_BRB_PLAYER, "browser_source", settings, on)
    else:
        cl.set_input_settings(SOURCE_BRB_PLAYER, settings, True)
        item = cl.get_scene_item_id(SCENE_BRB, SOURCE_BRB_PLAYER).scene_item_id
        cl.set_scene_item_enabled(SCENE_BRB, item, on)
        if on:
            cl.press_input_properties_button(SOURCE_BRB_PLAYER, "refreshnocache")
    try:
        cl.set_input_mute(SOURCE_BRB_MEDIA, on)
    except Exception:
        pass  # BRB media not bootstrapped yet - nothing to mute


def apply_brb_player_on_boot():
    """The source only needs creating once per OBS config volume, but OBS (and
    the watchdog's BRB scene) may come up after this process, so retry for a
    while. Created even with the player off - hidden - so turning it on later
    is just a visibility toggle."""
    for _ in range(200):
        player = load_config()["brb_player"]
        try:
            with_obs(lambda cl: apply_brb_player(cl, player))
            return
        except Exception:
            time.sleep(6)


threading.Thread(target=apply_brb_player_on_boot, daemon=True).start()


class BrbPlayerReq(BaseModel):
    mode: str
    youtube_list: str = ""
    clips_channel: str = ""
    clips_range: str = "7d"
    clips_pick: str = "top"
    clips_max_len: int = 0
    clips_len_mode: str = "skip"
    clips_exclude: str = ""
    clips_creators: str = ""
    overlay_info: bool = True
    overlay_timer: bool = False
    shuffle: bool = True
    volume: int = 60


def parse_playlist(value):
    """Accept a playlist URL (any youtube.com link with list=) or a bare ID."""
    value = value.strip()
    if not value:
        return ""
    if "://" in value or "youtube" in value or "youtu.be" in value:
        lst = parse_qs(urlparse(value if "://" in value else "https://" + value).query).get("list", [""])[0]
    else:
        lst = value
    if not YT_LIST_RE.match(lst):
        raise HTTPException(status_code=400, detail="not a YouTube playlist link (needs list=...)")
    return lst


@app.get("/api/brb-player")
def get_brb_player():
    return {**load_config()["brb_player"], "clips": dict(_clips_status),
            "last_event": _player_events[-1] if _player_events else None}


@app.post("/api/brb-player")
def set_brb_player(body: BrbPlayerReq):
    if body.mode not in PLAYER_MODES:
        raise HTTPException(status_code=400, detail="unknown mode")
    if body.clips_range not in CLIP_RANGES:
        raise HTTPException(status_code=400, detail="unknown clip range")
    if body.clips_pick not in CLIP_PICKS:
        raise HTTPException(status_code=400, detail="unknown clip pick")
    if body.clips_max_len not in CLIP_MAX_LENS:
        raise HTTPException(status_code=400, detail="unknown clip length")
    exclude = [w.strip() for w in body.clips_exclude.split(",") if w.strip()][:30]
    if any(len(w) > 40 for w in exclude):
        raise HTTPException(status_code=400, detail="exclude words: 40 characters max each")
    creators = [c.strip().lstrip("@") for c in body.clips_creators.split(",") if c.strip()][:30]
    if any(not CLIPPER_RE.match(c) for c in creators):
        raise HTTPException(status_code=400, detail="clippers: Twitch user names, comma separated")
    youtube_list = parse_playlist(body.youtube_list)
    channel = body.clips_channel.strip().lstrip("@").lower()
    if channel.startswith("http") or "twitch.tv/" in channel:
        channel = channel.split("twitch.tv/")[-1].split("/")[0].split("?")[0]
    if channel and not TWITCH_LOGIN_RE.match(channel):
        raise HTTPException(status_code=400, detail="not a Twitch channel name")
    if body.mode in ("youtube", "both") and not youtube_list:
        raise HTTPException(status_code=400, detail="YouTube playlist link required")
    if body.mode in ("clips", "both") and not channel:
        raise HTTPException(status_code=400, detail="Twitch channel required")

    with _cfg_lock:
        cfg = load_config()
        old = cfg["brb_player"]
        cfg["brb_player"] = {
            "mode": body.mode, "youtube_list": youtube_list,
            "clips_channel": channel, "clips_range": body.clips_range,
            "clips_pick": body.clips_pick, "clips_max_len": body.clips_max_len,
            "clips_len_mode": "cut" if body.clips_len_mode == "cut" else "skip",
            "clips_exclude": exclude, "clips_creators": creators,
            "overlay_info": body.overlay_info, "overlay_timer": body.overlay_timer,
            "shuffle": body.shuffle, "volume": max(0, min(100, body.volume)),
        }
        save_config(cfg)
        player = cfg["brb_player"]

    if any(player[k] != old[k] for k in CLIP_SELECTION_KEYS) or not read_clip_index():
        threading.Thread(target=refresh_clips, daemon=True).start()
    with_obs(lambda cl: apply_brb_player(cl, player))
    return {**player, "clips": dict(_clips_status)}


@app.post("/api/brb-player/refresh-clips")
def refresh_clips_now():
    threading.Thread(target=refresh_clips, daemon=True).start()
    return {"ok": True}


# The page and its data. Reached by OBS directly (dashboard:8000); from the
# outside only through Caddy, i.e. behind the login like everything else.
@app.get("/brb/player")
def brb_player_page():
    return FileResponse("static/brb-player.html")


@app.get("/brb/player/state")
def brb_player_state():
    p = load_config()["brb_player"]
    clips = read_clip_index() if p["mode"] in ("clips", "both") else []
    return {
        "mode": p["mode"],
        "youtube_list": p["youtube_list"] if p["mode"] in ("youtube", "both") else "",
        "shuffle": p["shuffle"], "volume": p["volume"],
        "overlay_info": p["overlay_info"], "overlay_timer": p["overlay_timer"],
        "cut_at": p["clips_max_len"] if p["clips_len_mode"] == "cut" else 0,
        "clips": [{"src": f"/brb/clips/{c['file']}", "title": c.get("title", ""),
                   "duration": c.get("duration", 0), "clipper": c.get("clipper", ""),
                   "game": c.get("game", ""), "timestamp": c.get("timestamp", 0)}
                  for c in clips],
    }


_player_events: list[str] = []


@app.post("/brb/player/log")
async def brb_player_log(request: Request):
    """The page inside OBS has no console anyone can see; it reports what it
    plays and what fails here. Last 20 kept, shown in the dashboard."""
    msg = (await request.body()).decode("utf-8", "replace")[:300]
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(f"[brb-player] {msg}", flush=True)
    _player_events.append(line)
    del _player_events[:-20]
    return {"ok": True}


app.mount("/brb/clips", StaticFiles(directory=str(CLIPS_DIR)), name="clips")


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
