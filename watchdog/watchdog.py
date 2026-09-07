"""BRB failover watchdog - the "never drop again" logic.

Polls the ingest container's sls stats endpoint to decide whether the
phone's/encoder's ingest is genuinely alive (publisher present AND bitrate
> 0 - sls recomputes that every second, so a stalled-but-connected input
reads as dead within a couple of ticks), and drives OBS scene switching over
obs-websocket:

    <a live scene> --(ingest down >= DOWN_THRESHOLD_SEC)--> BRB
    BRB --(ingest healthy >= UP_THRESHOLD_SEC)--> restart media source
        --> back to the live scene you were on when it dropped

There can be several live scenes (different camera/PiP layouts), managed from
the dashboard, which owns the list and writes it to the shared config file.
They are interchangeable as far as failover is concerned: any of them means
"on air". The one you were last on is remembered, so a BRB detour returns you
to that layout rather than dumping you back on the default one - switching
scenes mid-stream shouldn't be undone by a dropped connection.

Also bootstraps the LIVE/BRB scenes and sources in OBS on startup (idempotent),
so a fresh OBS container self-configures instead of relying on hand-written
scene-collection JSON. Exposes its state as JSON on :8081 for the dashboard.

Fork note: this used to poll MediaMTX's `/v3/paths/get/{path}` (bytesReceived
counter). The ingest container is now the streamserver-derived sls stack
instead of MediaMTX - sls has no cumulative byte counter, only an
instantaneous bitrate, so the health signal changed from "bytes still
increasing" to "bitrate still nonzero". See ingest/conf/sls.conf for the
matching idle_streams_timeout.
"""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import obsws_python as obsws

log = logging.getLogger("watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OBS_HOST = os.environ.get("OBS_HOST", "obs")
OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD = os.environ["OBS_WS_PASSWORD"]
INGEST_STATS_URL = os.environ.get("INGEST_STATS_URL", "http://ingest:8080")
PLAY_KEY = os.environ["PLAY_KEY"]
SRT_READ_URL = os.environ.get(
    "SRT_READ_URL", f"srt://ingest:4000?streamid={PLAY_KEY}&latency=2000000"
)
TICK_SEC = float(os.environ.get("TICK_SEC", "0.5"))
DOWN_THRESHOLD = float(os.environ.get("DOWN_THRESHOLD_SEC", "1.5"))
UP_THRESHOLD = float(os.environ.get("UP_THRESHOLD_SEC", "3.0"))
MIN_LIVE_DWELL = float(os.environ.get("MIN_LIVE_DWELL_SEC", "5.0"))

# The live scene that always exists. The dashboard can add more alongside it,
# but this one is never removable - something has to be there to fall back to
# if the config file is empty, unreadable, or half-written.
SCENE_LIVE_DEFAULT = "LIVE"
SCENE_BRB = "BRB"

# Written by the dashboard, read-only here (see docker-compose.yml). Kept as
# a plain shared file rather than an HTTP call so that a dashboard restart
# can never take the failsafe down with it.
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/data/config.json"))
SOURCE_INGEST = "Ingest"
SOURCE_BRB_MEDIA = "BRB Media"
SOURCE_BRB_IMAGE = "BRB Image"
# Matches the OBS profile's Base/Output resolution - keep both in sync, or
# the encoder downscales every single frame for nothing.
#
# 1080p here is a deliberate choice for this Hetzner box (i7-7700, 4c/8t),
# not the upstream default. The 1280x720 this used to hold was sized for a
# 2-OCPU Oracle Ampere instance, where software-rendered compositing at
# 1080p pegged both cores even with nothing streaming. Compositing still
# runs on llvmpipe here (LIBGL_ALWAYS_SOFTWARE=1 - docker-compose.igpu.yml
# hands over the encoder only), so the cost scales with canvas pixels and
# this roughly doubles it; on 8 threads that is affordable. Watch `uptime`
# under load - see docs/setup-hetzner.md §8.
CANVAS_W, CANVAS_H = 1920, 1080

# Shared with the HTTP status server.
state = {
    "scene": "unknown",
    # Every scene that counts as on air, and the one a BRB detour returns to.
    "live_scenes": [],
    "last_live_scene": None,
    # False while the operator has paused automatic scene switching.
    "failover_enabled": True,
    "ingest_healthy": False,
    "ingest_bitrate_kbps": 0,
    "obs_connected": False,
    "last_transition": None,
    "transitions": 0,
}
state_lock = threading.Lock()


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock:
            body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class IngestMonitor:
    """Health = publisher present AND bitrate > 0 (sls updates it every 1s)."""

    def healthy(self):
        try:
            r = requests.get(f"{INGEST_STATS_URL}/stats/{PLAY_KEY}", timeout=2)
            if r.status_code != 200:
                return False
            info = r.json()
            if info.get("status") != "ok":
                return False
            publisher = info.get("publisher")
            if not isinstance(publisher, dict):
                return False
            bitrate = publisher.get("bitrate") or 0
            set_state(ingest_bitrate_kbps=int(bitrate))
            return bitrate > 0
        except requests.RequestException:
            return False


def connect_obs():
    while True:
        try:
            cl = obsws.ReqClient(
                host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5
            )
            set_state(obs_connected=True)
            log.info("connected to obs-websocket at %s:%s", OBS_HOST, OBS_PORT)
            return cl
        except Exception as e:
            set_state(obs_connected=False)
            log.warning("obs not reachable yet (%s), retrying in 3s", e)
            time.sleep(3)


def load_watch_config():
    """(live_scenes, failover_enabled) as the dashboard currently has them.

    Read fresh on every use rather than cached: edits in the dashboard have to
    take effect without restarting the watchdog, and this is one small local
    file read per tick.

    Defensive on purpose, because this decides whether the failsafe runs at
    all, and every failure mode here has to land on the safe side:

    * A missing, unreadable or half-written file degrades to the default
      scene with failover ON, rather than raising.
    * SCENE_LIVE_DEFAULT is always present, so there is always somewhere to
      return to.
    * BRB is filtered out of the live scenes - a config entry naming BRB as
      live would otherwise make the watchdog treat the BRB screen as being on
      air and never switch back.
    * Only an explicit `false` disables failover. Anything else - key absent,
      null, a typo, a truncated write - leaves it enabled. Silently losing
      the failsafe is far worse than an ignored toggle.
    """
    scenes, enabled = [], True
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        raw = cfg.get("live_scenes")
        if isinstance(raw, list):
            scenes = [x for x in raw if isinstance(x, str) and x]
        enabled = cfg.get("failover_enabled") is not False
    except (OSError, ValueError) as e:
        log.debug("watch config unreadable (%s), falling back to defaults", e)
    out = [x for x in scenes if x != SCENE_BRB]
    if SCENE_LIVE_DEFAULT not in out:
        out.insert(0, SCENE_LIVE_DEFAULT)
    return out, enabled


def load_live_scenes():
    return load_watch_config()[0]


def stretch_to_canvas(cl, scene, source):
    item_id = cl.get_scene_item_id(scene, source).scene_item_id
    cl.set_scene_item_transform(
        scene,
        item_id,
        {
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "boundsWidth": CANVAS_W,
            "boundsHeight": CANVAS_H,
            "positionX": 0,
            "positionY": 0,
        },
    )


def ensure_ingest_in(cl, scene):
    """Put the shared Ingest source into a live scene if it isn't there yet.

    One input, referenced from every live scene - not a copy per scene. That
    matters: a second ffmpeg_source would open a second SRT session against
    sls, which only serves one player per stream id, and the layouts would
    drift out of sync with each other anyway.

    The dashboard does this when it creates a scene; repeating it here covers
    scenes added straight in the OBS UI and anything the dashboard failed
    halfway through.
    """
    try:
        cl.get_scene_item_id(scene, SOURCE_INGEST)
        return False
    except Exception:
        cl.create_scene_item(scene, SOURCE_INGEST, True)
        try:
            stretch_to_canvas(cl, scene, SOURCE_INGEST)
        except Exception as e:
            # Cosmetic only - the source is in the scene either way, and the
            # user is free to re-position it for a PiP layout.
            log.warning("could not fit %s into %s (%s)", SOURCE_INGEST, scene, e)
        log.info("added %s to live scene %s", SOURCE_INGEST, scene)
        return True


def ensure_scenes(cl, live_scenes=None):
    """Idempotently create the live/BRB scenes and their sources."""
    live_scenes = live_scenes or load_live_scenes()
    existing = {s["sceneName"] for s in cl.get_scene_list().scenes}
    for scene in (*live_scenes, SCENE_BRB):
        if scene not in existing:
            cl.create_scene(scene)
            log.info("created scene %s", scene)

    def input_names():
        return {i["inputName"] for i in cl.get_input_list().inputs}

    if SOURCE_INGEST not in input_names():
        cl.create_input(
            SCENE_LIVE_DEFAULT,
            SOURCE_INGEST,
            "ffmpeg_source",
            {
                "input": SRT_READ_URL,
                "is_local_file": False,
                "buffering_mb": 2,
                "reconnect_delay_sec": 1,
                "restart_on_activate": False,
                "clear_on_media_end": False,
                "hw_decode": False,
            },
            True,
        )
        stretch_to_canvas(cl, SCENE_LIVE_DEFAULT, SOURCE_INGEST)
        log.info("created ingest media source -> %s", SRT_READ_URL)

    if SOURCE_BRB_MEDIA not in input_names():
        cl.create_input(
            SCENE_BRB,
            SOURCE_BRB_MEDIA,
            "ffmpeg_source",
            {"is_local_file": True, "local_file": "", "looping": True},
            True,
        )
        stretch_to_canvas(cl, SCENE_BRB, SOURCE_BRB_MEDIA)

    if SOURCE_BRB_IMAGE not in input_names():
        cl.create_input(
            SCENE_BRB, SOURCE_BRB_IMAGE, "image_source", {"file": ""}, False
        )
        stretch_to_canvas(cl, SCENE_BRB, SOURCE_BRB_IMAGE)

    # Every live scene shows the same ingest; the default one got it via
    # create_input above, the rest need it referenced in.
    for scene in live_scenes:
        ensure_ingest_in(cl, scene)

    current = cl.get_current_program_scene().current_program_scene_name
    if current not in (*live_scenes, SCENE_BRB):
        cl.set_current_program_scene(SCENE_LIVE_DEFAULT)
        current = SCENE_LIVE_DEFAULT
    set_state(scene=current, live_scenes=live_scenes)
    return current


def run():
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", 8081), StatusHandler).serve_forever,
        daemon=True,
    ).start()

    monitor = IngestMonitor()
    cl = connect_obs()
    live_scenes, failover_on = load_watch_config()
    set_state(failover_enabled=failover_on)
    scene = ensure_scenes(cl, live_scenes)

    # Which live scene to come back to after a BRB detour. Seeded from
    # whatever OBS is actually showing, so a watchdog restart mid-stream
    # doesn't yank the operator back to the default layout.
    last_live = scene if scene in live_scenes else SCENE_LIVE_DEFAULT

    down_since = None
    up_since = None
    live_entered_at = time.monotonic()
    # Anti-flap: if the last stint on air was shorter than MIN_LIVE_DWELL,
    # the connection is bouncing - demand a longer stable period before the
    # next return, so viewers see a steady BRB instead of strobing scenes.
    # Measured across live scenes as a group; switching layouts by hand is
    # not a reconnect and must not reset this.
    required_up = UP_THRESHOLD

    while True:
        time.sleep(TICK_SEC)
        healthy = monitor.healthy()
        if not healthy:
            set_state(ingest_bitrate_kbps=0)
        set_state(ingest_healthy=healthy)
        now = time.monotonic()

        try:
            # Pick up dashboard edits to the live-scene list without a
            # restart, and give any newly added scene the ingest source.
            new_live, now_on = load_watch_config()
            if new_live != live_scenes:
                log.info("live scenes changed: %s -> %s", live_scenes, new_live)
                for name in new_live:
                    if name not in live_scenes:
                        try:
                            ensure_ingest_in(cl, name)
                        except Exception as e:
                            log.warning("could not prepare live scene %s (%s)",
                                        name, e)
                live_scenes = new_live
                if last_live not in live_scenes:
                    # The scene we were going to return to just got deleted.
                    last_live = SCENE_LIVE_DEFAULT
                set_state(live_scenes=live_scenes)

            scene = cl.get_current_program_scene().current_program_scene_name
            set_state(scene=scene)

            # Failover paused from the dashboard. Keep reporting health and
            # the current scene - the dashboard stays useful - but touch
            # nothing. Needed to build or edit a scene at all: with the
            # ingest down (which it is whenever the phone isn't running) the
            # watchdog otherwise drags the program scene back to BRB every
            # couple of seconds, and you cannot place an overlay or an alert
            # browser source on a scene that keeps disappearing.
            if now_on != failover_on:
                log.warning("failover %s", "resumed" if now_on else "PAUSED from dashboard")
                failover_on = now_on
                set_state(failover_enabled=failover_on)
            if not failover_on:
                # Drop the timers, so resuming starts from a clean slate
                # instead of firing instantly on a threshold that quietly
                # matured while switching was paused.
                down_since = None
                up_since = None
                continue

            # Respect manual control: if the user switched to some scene that
            # isn't registered as live and isn't BRB (starting-soon,
            # intermission, ...) from the dashboard or OBS, stand down until
            # they come back.
            if scene not in live_scenes and scene != SCENE_BRB:
                down_since = None
                up_since = None
                continue

            if scene in live_scenes:
                # Remember the layout in use, so the trip back from BRB
                # returns here rather than to the default live scene.
                if scene != last_live:
                    last_live = scene
                    set_state(last_live_scene=last_live)
                up_since = None
                if healthy:
                    down_since = None
                else:
                    down_since = down_since or now
                    if now - down_since >= DOWN_THRESHOLD:
                        log.warning("ingest down %.1fs -> switching to BRB",
                                    now - down_since)
                        flapping = now - live_entered_at < MIN_LIVE_DWELL
                        required_up = UP_THRESHOLD * (3 if flapping else 1)
                        cl.set_current_program_scene(SCENE_BRB)
                        scene = SCENE_BRB
                        set_state(scene=scene, transitions=state["transitions"] + 1,
                                  last_transition=time.strftime("%Y-%m-%dT%H:%M:%S"))
            else:  # BRB
                down_since = None
                if not healthy:
                    up_since = None
                else:
                    up_since = up_since or now
                    if now - up_since >= required_up:
                        log.info("ingest healthy %.1fs -> restarting source, back to %s",
                                 now - up_since, last_live)
                        # Re-latch onto the fresh SRT publisher session.
                        cl.trigger_media_input_action(
                            SOURCE_INGEST,
                            "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
                        )
                        time.sleep(1.0)
                        target = last_live if last_live in live_scenes \
                            else SCENE_LIVE_DEFAULT
                        cl.set_current_program_scene(target)
                        scene = target
                        live_entered_at = time.monotonic()
                        up_since = None
                        set_state(scene=scene, transitions=state["transitions"] + 1,
                                  last_transition=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            log.error("obs call failed (%s) - reconnecting", e)
            set_state(obs_connected=False)
            cl = connect_obs()
            live_scenes, failover_on = load_watch_config()
            set_state(failover_enabled=failover_on)
            scene = ensure_scenes(cl, live_scenes)


if __name__ == "__main__":
    run()
