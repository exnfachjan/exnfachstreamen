#!/usr/bin/env bash
# Shared helpers for install.sh and the host profiles.
#
# Sourced, never executed. Everything here is deliberately chatty: this script
# edits boot configuration and installs a Docker daemon on someone's server,
# and the person running it should be able to see what changed and undo it.

set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
[ -t 1 ] || { RED=; GRN=; YLW=; DIM=; BLD=; RST=; }

step()  { printf '\n%s==>%s %s%s%s\n' "$BLD" "$RST" "$BLD" "$*" "$RST"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s+%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '    %s!%s %s\n' "$YLW" "$RST" "$*"; }
die()   { printf '\n%serror:%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
skip()  { printf '    %s- %s%s\n' "$DIM" "$*" "$RST"; }

# Every prompt has a default that is safe to accept, so a distracted operator
# holding Enter ends up with a working install rather than a broken one.
ask() {
    local prompt="$1" default="${2-}" reply
    if [ "${ASSUME_YES:-0}" = 1 ]; then printf '%s' "$default"; return; fi
    if [ -n "$default" ]; then
        read -r -p "    $prompt [$default]: " reply </dev/tty || true
        printf '%s' "${reply:-$default}"
    else
        read -r -p "    $prompt: " reply </dev/tty || true
        printf '%s' "$reply"
    fi
}

confirm() {
    local prompt="$1" default="${2:-y}" reply
    if [ "${ASSUME_YES:-0}" = 1 ]; then [ "$default" = y ]; return; fi
    read -r -p "    $prompt [$([ "$default" = y ] && echo 'Y/n' || echo 'y/N')]: " reply </dev/tty || true
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[YyJj] ]]
}

# Timestamped backup before touching anything the OS owns. Not .bak alone:
# re-running the installer must never overwrite the pristine original with an
# already-modified copy.
backup_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    local b
    b="${f}.pre-exnfachstreamen.$(date +%Y%m%d-%H%M%S)"
    cp -a "$f" "$b"
    info "backup: $b"
}

need_root() { [ "$(id -u)" -eq 0 ] || die "run this as root (or with sudo)"; }

have() { command -v "$1" >/dev/null 2>&1; }

# The address the outside world reaches this machine on. Used to check the A
# record before Caddy ever talks to Let's Encrypt, because a wrong record there
# costs an hour of rate-limit rather than an error message.
public_ipv4() {
    ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}

# ---------------------------------------------------------------- .env ---
env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

# Set KEY=VALUE in .env, replacing an existing line or appending a new one.
# VALUE goes through awk -v, not sed, so a token full of / + = cannot break it.
env_set() {
    local key="$1" val="$2" tmp
    tmp="$(mktemp)"
    if grep -qE "^$key=" .env; then
        awk -v k="$key" -v v="$val" 'index($0, k"=")==1 {print k"="v; next} {print}' .env > "$tmp"
    else
        cat .env > "$tmp"; printf '%s=%s\n' "$key" "$val" >> "$tmp"
    fi
    cat "$tmp" > .env; rm -f "$tmp"   # cat, not mv: keeps .env's mode 600
}

# Fill in the Cloudflare-tunnel keys on an .env written before they existed
# (or with the template's placeholder still in it). Shared by install.sh and
# update.sh, so an existing server migrates on its next update.
ensure_tunnel_env() {
    local cur
    cur="$(env_get TUNNEL_TOKEN)"
    if [ -z "$cur" ] || [ "$cur" = change-me ]; then
        info "Cloudflare Tunnel token: Zero Trust -> Networks -> Tunnels ->"
        info "Create a tunnel (Cloudflared) -> copy the string after --token"
        cur="$(ask 'Tunnel token')"
        [ -n "$cur" ] || die "TUNNEL_TOKEN is required - cloudflared is the only way to reach the panel"
        env_set TUNNEL_TOKEN "$cur"
        ok "TUNNEL_TOKEN saved"
    else
        skip "TUNNEL_TOKEN already set"
    fi

    cur="$(env_get INGEST_HOST)"
    if [ -z "$cur" ] || [ "$cur" = ingest.example.com ]; then
        info "Phones need a separate DNS-only (grey cloud) hostname for SRT/SRTLA -"
        info "UDP does not go through the tunnel."
        cur="$(ask 'Ingest hostname (A record -> this server, grey cloud)' "ingest.$(env_get DOMAIN)")"
        env_set INGEST_HOST "$cur"
        ok "INGEST_HOST=$cur"
    else
        skip "INGEST_HOST=$cur"
    fi
}

# ----------------------------------------------------------- resolution ---
# Canvas size lives in two tracked files that install.sh edits in place. Those
# edits are local modifications as far as git is concerned, so update.sh reads
# them out first, resets the files, pulls, and writes them back with this.
current_resolution() {
    sed -n 's/^BaseCY=\([0-9]*\).*/\1/p' obs/config/profile-basic.ini | head -1
}

apply_resolution() {
    # 1080 is the quality preset: 60 fps at 7000 kbps. Above Twitch's
    # published 6000 recommendation, but accepted by its ingest; check the
    # stream health in Twitch Inspector after changing it.
    local cw ch fps vb
    case "$1" in
        720)  cw=1280; ch=720;  fps=30; vb=4500 ;;
        1080) cw=1920; ch=1080; fps=60; vb=7000 ;;
        *) die "resolution must be 720 or 1080" ;;
    esac
    sed -i -E "s/^BaseCX=.*/BaseCX=$cw/;   s/^BaseCY=.*/BaseCY=$ch/;
               s/^OutputCX=.*/OutputCX=$cw/; s/^OutputCY=.*/OutputCY=$ch/;
               s/^FPSCommon=.*/FPSCommon=$fps/;
               s/^VBitrate=.*/VBitrate=$vb/" obs/config/profile-basic.ini
    sed -i -E "s/^CANVAS_W, CANVAS_H = .*/CANVAS_W, CANVAS_H = $cw, $ch/" watchdog/watchdog.py
    ok "${cw}x${ch}@${fps} @ ${vb} kbps, written to the OBS seed and the watchdog"
}

# nginx resolves the dashboard hostname for its on_publish callback once, at
# config load, and caches the container IP. If the dashboard was recreated
# after ingest, RTMP publishes get rejected with nothing in any log to show it.
restart_ingest_after_up() {
    sleep 5
    docker compose restart ingest >/dev/null
}

# --------------------------------------------------------------- app user ---
# The stack gets its own system account: it owns the checkout, .env and
# data/, has no password and no login shell, and is in the docker group so
# `sudo -u <user> docker compose ...` works without root. Created by
# install.sh, remembered in .env as APP_USER for update.sh and uninstall.sh.
#
# The containers themselves are still started by the Docker daemon, as
# before - this account separates the files, not the container processes.
APP_USER_COMMENT="exnfachstreamen app user"
APP_USER_DEFAULT=exnfachstreamen

ensure_app_user() {
    local u="$1" home="/var/lib/$1"
    if id "$u" >/dev/null 2>&1; then
        skip "user $u exists"
    else
        useradd --system --user-group --create-home --home-dir "$home" \
                --shell /usr/sbin/nologin --comment "$APP_USER_COMMENT" "$u"
        ok "created system user $u (no password, no login shell)"
    fi
    getent group docker >/dev/null && usermod -aG docker "$u"
}

# Everything in the checkout back to the app user. Re-run after anything that
# writes as root: git pulls, the installer's edits, and the containers, which
# create files under data/ as root.
chown_app() {
    local u="${1:-}"
    [ -n "$u" ] && id "$u" >/dev/null 2>&1 || return 0
    chown -R "$u:$u" .
    [ -f .env ] && chmod 600 .env
    return 0
}

# git run as root on a checkout owned by the app user: git refuses that
# ("dubious ownership") unless the directory is marked safe for this call.
# Root, not the app user, so a deploy key in /root/.ssh keeps working.
g() { git -c safe.directory="$PWD" "$@"; }
