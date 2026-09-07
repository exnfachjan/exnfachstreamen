#!/bin/sh
# Polls the Twitch push-target file the dashboard writes and reloads nginx
# when it changes. Simple mtime+size poll instead of inotify - one less
# package, and a 3s worst-case delay before a newly added channel goes live
# doesn't matter here.
set -e

TARGETS=/etc/nginx/restream.d/targets.conf
last=""

while true; do
    if [ -f "$TARGETS" ]; then
        cur=$(stat -c '%Y-%s' "$TARGETS" 2>/dev/null || echo "")
        if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
            if [ -n "$last" ]; then
                echo "[reload-watcher] targets.conf changed, reloading nginx"
                nginx -s reload -c /etc/nginx/nginx.conf 2>&1 || \
                    echo "[reload-watcher] reload failed (nginx not up yet?)"
            fi
            last="$cur"
        fi
    fi
    sleep 3
done
