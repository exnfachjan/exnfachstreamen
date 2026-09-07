#!/bin/bash
set -e

CONF="$HOME/.config/obs-studio"
mkdir -p "$CONF/basic/profiles/IRL" "$CONF/basic/scenes"

# Seed profile + service config on first run only (dashboard mutates these later
# via obs-websocket; never clobber them on restart).
if [ ! -f "$CONF/basic/profiles/IRL/basic.ini" ]; then
    cp /opt/obs-config-seed/profile-basic.ini "$CONF/basic/profiles/IRL/basic.ini"
    cp /opt/obs-config-seed/service.json      "$CONF/basic/profiles/IRL/service.json"
fi

# OBS 31 split its config: global.ini holds only the migration marker (without
# it, OBS shows a blocking "unable to migrate" dialog on startup); user prefs
# live in user.ini; obs-websocket reads plugin_config/obs-websocket/config.json.
# All rewritten every start so OBS_WS_PASSWORD from .env always wins.
#
# That rewrite is also why global.ini.tmpl sets FirstRun=true. The key reads
# backwards: it means "the first run has already happened", and OBS offers its
# auto-configuration wizard when it is false. OBS flips it to true itself right
# after showing the wizard, but rewriting user.ini on every start put false
# back, so the wizard returned after every restart and rebuild.
printf '[General]\nPre31Migrated=true\n' > "$CONF/global.ini"
envsubst < /opt/obs-config-seed/global.ini.tmpl > "$CONF/user.ini"
mkdir -p "$CONF/plugin_config/obs-websocket"
cat > "$CONF/plugin_config/obs-websocket/config.json" <<EOF
{
    "alerts_enabled": false,
    "auth_required": true,
    "first_load": false,
    "server_enabled": true,
    "server_password": "${OBS_WS_PASSWORD}",
    "server_port": 4455
}
EOF

# Clear a stale X lock before starting the display server.
#
# `docker compose stop obs` (or any SIGTERM/SIGKILL) kills Xkasmvnc without
# letting it clean up, and `docker compose up -d obs` afterwards restarts the
# SAME container with its writable layer - /tmp included. The leftover lock
# then makes the X server refuse to start with "Server is already active for
# display 99", OBS finds no display, Qt aborts, and the container dies on
# loop with exit 139. Nothing in OBS's own log explains it, because OBS never
# gets far enough to open one - the only trace is on container stdout.
#
# Safe unconditionally: this runs as PID 1's child in a fresh container
# process tree, so any :99 server the lock refers to is already gone.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# KasmVNC is both the virtual display and the browser remote desktop
# (server-side WebP/JPEG encode, web client on :6080). Auth is disabled here
# because caddy's forward_auth gates /obs/* with the dashboard login.
Xkasmvnc :99 -geometry 1920x1080 -depth 24 \
    -websocketPort 6080 -interface 0.0.0.0 \
    -httpd /usr/share/kasmvnc/www \
    -SecurityTypes None -DisableBasicAuth -FrameRate 60 &

# Dummy audio server so OBS's mixer has somewhere to live.
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit || true

# Sunshine: high-quality remote desktop via Moonlight clients. Software
# x264 encode of the same virtual display. Web UI (pairing) on :47990,
# credentials admin / $OBS_WS_PASSWORD unless SUNSHINE_PASSWORD is set.
mkdir -p "$HOME/.config/sunshine"
if [ ! -f "$HOME/.config/sunshine/sunshine.conf" ]; then
    cat > "$HOME/.config/sunshine/sunshine.conf" <<EOF
capture = x11
encoder = software
EOF
fi
sunshine --creds admin "${SUNSHINE_PASSWORD:-$OBS_WS_PASSWORD}" >/dev/null 2>&1 || true
sunshine >/var/log/sunshine.log 2>&1 &

# Give Xvfb a moment before OBS attaches to it.
sleep 2

exec obs --collection IRL --profile IRL \
     --disable-shutdown-check --disable-missing-files-check
