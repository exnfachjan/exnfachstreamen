#!/bin/sh
# Brings up sls just long enough to (1) let it create its own SQLite DB on
# first run and (2) register our one publisher/player key pair via its own
# HTTP API, then hands off to supervisord for the real, long-running
# processes. Mirrors streamserver's entrypoint.sh, minus everything that
# existed only to feed the Django panel.
set -e

STATE=/var/lib/sls
KEYFILE="$STATE/api_key"
DB="$STATE/streams.db"

: "${PUBLISH_KEY:?PUBLISH_KEY must be set (see .env.example)}"
: "${PLAY_KEY:?PLAY_KEY must be set (see .env.example)}"

mkdir -p "$STATE"
chown sls:sls "$STATE" 2>/dev/null || true

# The restream.d volume can come up empty (Docker's "seed from image"
# behaviour only reliably fires for whichever container mounts it first,
# and dashboard's image has nothing at that path) - nginx's `include` below
# fails hard on a missing file, so make sure it exists no matter what.
mkdir -p /etc/nginx/restream.d
[ -f /etc/nginx/restream.d/targets.conf ] || : > /etc/nginx/restream.d/targets.conf

if [ ! -f "$DB" ]; then
    echo "[init] first start - creating sls database ..."
    su -s /bin/sh sls -c "/usr/local/bin/sls -c /etc/sls/sls.conf" > /tmp/sls-init.log 2>&1 &
    INITPID=$!
    i=0
    while [ $i -lt 30 ] && [ ! -f "$DB" ]; do
        i=$((i+1))
        sleep 1
    done
    if [ ! -f "$DB" ]; then
        echo "[init] ERROR: $DB was not created. Log:"
        tail -20 /tmp/sls-init.log
        kill "$INITPID" 2>/dev/null || true
        exit 1
    fi

    # apikey.py needs the DB; the stream-id bootstrap below needs the HTTP
    # API up. Both are true right now, while this throwaway instance is
    # still running from the DB-creation wait above.
    /usr/bin/python3 /usr/local/bin/apikey.py "$DB" "$KEYFILE"
    chown sls:sls "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true
    API_KEY=$(cat "$KEYFILE")

    echo "[init] registering publish/play key pair ..."
    j=0
    until curl -sf -o /dev/null "http://127.0.0.1:8080/health" || [ $j -ge 15 ]; do
        j=$((j+1)); sleep 1
    done
    curl -sf -X POST "http://127.0.0.1:8080/api/stream-ids" \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -d "{\"publisher\":\"$PUBLISH_KEY\",\"player\":\"$PLAY_KEY\",\"description\":\"main\"}" \
        || echo "[init] stream-id registration failed or already exists - continuing"

    kill "$INITPID" 2>/dev/null || true
    wait "$INITPID" 2>/dev/null || true
else
    # Not the first start: still keep the key file honest against the DB,
    # and make sure our pair exists (e.g. PUBLISH_KEY changed in .env, or a
    # restored backup is missing it) once sls is up under supervisord.
    /usr/bin/python3 /usr/local/bin/apikey.py "$DB" "$KEYFILE"
    chown sls:sls "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true
    (
        API_KEY=$(cat "$KEYFILE")
        j=0
        until curl -sf -o /dev/null "http://127.0.0.1:8080/health" || [ $j -ge 30 ]; do
            j=$((j+1)); sleep 1
        done
        curl -sf -X POST "http://127.0.0.1:8080/api/stream-ids" \
            -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
            -d "{\"publisher\":\"$PUBLISH_KEY\",\"player\":\"$PLAY_KEY\",\"description\":\"main\"}" \
            >/dev/null 2>&1 || true
    ) &
fi

exec /usr/bin/supervisord -c /etc/supervisord.conf
