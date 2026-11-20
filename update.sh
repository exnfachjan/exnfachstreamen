#!/usr/bin/env bash
# exnfachstreamen updater.
#
# Pulls the latest code, migrates .env if new keys were added, pulls the
# prebuilt images (caddy, cloudflared), rebuilds ours, and restarts only what
# changed. Your .env, data/ and every Docker volume (OBS scenes, sls keys,
# Twitch targets) are kept.
#
#   ./update.sh              # update and restart (as root)
#   ./update.sh --no-pull    # skip git pull (rebuild the current checkout)
#   ./update.sh --yes        # no prompts
#
# A running stream WILL drop for the few seconds the changed containers need
# to restart. Run it between streams.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
. install/lib.sh

ASSUME_YES=0; PULL=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-pull) PULL=0; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

need_root
[ -f docker-compose.yml ] || die "run this from the repository root"
[ -f .env ] || die "no .env - this is not installed yet; run ./install.sh"
have docker || die "docker not found"

APP_USER="$(env_get APP_USER)"

# ------------------------------------------------------------------- code ---
# git runs as root (g() in install/lib.sh) and the checkout is handed back to
# the app user afterwards - see chown_app below.
step "Code"
BEFORE="$(g rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ "$PULL" = 1 ]; then
    # install.sh writes the canvas size into two tracked files. Remember it,
    # put the files back to their committed state so the pull cannot conflict
    # on them, and write it again afterwards.
    RES="$(current_resolution)"
    g checkout -- obs/config/profile-basic.ini watchdog/watchdog.py
    if ! g diff --quiet || ! g diff --cached --quiet; then
        warn "uncommitted changes besides the canvas size:"
        g status --short | sed 's/^/      /'
        apply_resolution "$RES"
        die "commit or stash them first (git stash), then run this again"
    fi
    g pull --ff-only || { apply_resolution "$RES"; die "git pull failed - nothing was changed"; }
    apply_resolution "$RES"
else
    skip "git pull skipped (--no-pull)"
fi
AFTER="$(g rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ "$BEFORE" = "$AFTER" ]; then
    skip "already at $AFTER"
else
    ok "$BEFORE -> $AFTER"
    g --no-pager log --oneline "$BEFORE..$AFTER" 2>/dev/null | sed 's/^/      /' || true
fi

# -------------------------------------------------------------------- env ---
step "Configuration (.env)"
ensure_tunnel_env

chown_app "$APP_USER"

# ----------------------------------------------------------------- images ---
step "Images"
docker compose pull --ignore-buildable 2>/dev/null || docker compose pull caddy cloudflared
docker compose build
ok "images up to date"

# ------------------------------------------------------------------ start ---
step "Restart"
confirm "Restart the changed containers now? (a live stream drops for a few seconds)" y \
    || { info "Not restarted. Later: docker compose up -d --remove-orphans"; exit 0; }
# --remove-orphans: drops services that no longer exist in the compose file
# (nothing to do on most updates).
docker compose up -d --remove-orphans
restart_ingest_after_up

chown_app "$APP_USER"

step "Cleanup"
# Only dangling layers left behind by the rebuild - never volumes, never
# tagged images. The OBS build leaves several GB of these each time.
docker image prune -f >/dev/null && ok "removed dangling images"

step "Done"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
