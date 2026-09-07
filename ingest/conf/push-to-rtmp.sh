#!/bin/sh
# Wrapper for nginx-rtmp's exec_push (see the `restream` application in
# nginx.conf): pulls the just-published stream back over localhost and
# pushes it to one external RTMP destination, connecting by IPv4 address
# instead of by hostname.
#
# Why: ffmpeg has no happy-eyeballs fallback between address families - it
# takes whichever address DNS returns first and gives up if that one is
# unreachable. Plenty of CDNs (Twitch's RTMP ingest included) publish AAAA
# records even on networks with no real outbound IPv6 route - true of Docker
# Desktop always, and of some real VPS hosts too - so a plain hostname
# connection can fail outright ("Network unreachable") even though the
# service is perfectly reachable over IPv4. Resolving to an IPv4 address
# ourselves sidesteps that; -rtmp_tcurl keeps the real hostname in the RTMP
# handshake in case the far end's routing/auth cares about it.
#
# args: $1 = source stream name (nginx's $name), $2 = destination host,
#       $3 = destination app, $4 = destination stream key
set -e

SRC_NAME="$1"
DEST_HOST="$2"
DEST_APP="$3"
DEST_KEY="$4"

IP=$(getent ahostsv4 "$DEST_HOST" 2>/dev/null | awk '{print $1; exit}')
if [ -z "$IP" ]; then
    echo "[push-to-rtmp] no IPv4 address for $DEST_HOST - trying the hostname directly (will fail here if the network has no IPv6 route)" >&2
    IP="$DEST_HOST"
fi

# -stats/-stats_period: this process is OBS's blind spot - OBS only ever
# sees its own connection to the local restream app, so its "0 skipped
# frames" can look perfectly clean while THIS hop (the actual link to
# Twitch) is the one stuttering, dropping, or stalling, with nothing to show
# it. ffmpeg's default -stats output is normally suppressed when stderr
# isn't a tty (true here - it goes to a log file), so it has to be forced.
#
# -fflags nobuffer -flags low_delay on the input, -rtmp_buffer 100 on the
# output: first real test showed `speed=` starting at 2x and decaying to
# ~1x over roughly a minute - a backlog present at process start being
# gradually drained, not a live-pace read. ffmpeg's rtmp muxer buffers 3000ms
# by default before it starts sending; combined with default input-side
# buffering that's enough to plausibly account for a real chunk of the
# reported ~30s glass-to-glass delay. This is a copy-relay of an already
# real-time source (OBS) - there is nothing to gain by buffering either end
# further, so cut both down as far as they go.
exec /usr/bin/ffmpeg -nostdin -loglevel warning -stats -stats_period 2 \
    -fflags nobuffer -flags low_delay \
    -i "rtmp://127.0.0.1:1935/restream/${SRC_NAME}" \
    -c copy -f flv -rtmp_buffer 100 \
    -rtmp_tcurl "rtmp://${DEST_HOST}/${DEST_APP}" \
    "rtmp://${IP}/${DEST_APP}/${DEST_KEY}"
