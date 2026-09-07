# ============================================================================
#  ingest - SRT + SRTLA + RTMP, single stream key, no panel.
#
#  Origin: trimmed fork of exnfachjan/streamserver. The Django panel (SLSPanel)
#  is gone - sls (srt-live-server) already has its own stream-id database and
#  REST API, the panel was only a UI wrapper around it. Here the `dashboard`
#  service (a sibling container, see ../dashboard) owns that one job instead,
#  so we don't run two web UIs.
#
#  Two stages: 1) pull the sls + srtla_rec binaries from the upstream image,
#  2) assemble the slim Alpine runtime (nginx-rtmp + ffmpeg + supervisor).
# ============================================================================

FROM alexanderwagnerdev/srtla-server:latest AS sls

FROM alpine:3.20

RUN apk add --no-cache \
        python3 nginx nginx-mod-rtmp ffmpeg supervisor curl \
        sqlite-libs libstdc++ tzdata && \
    addgroup -g 3001 sls   && adduser -D -H -u 3001 -G sls   sls   && \
    addgroup -g 3002 srtla && adduser -D -H -u 3002 -G srtla srtla && \
    mkdir -p /var/lib/sls /var/lib/nginx/logs /etc/nginx/restream.d && \
    chown -R nginx:nginx /var/lib/nginx && \
    chown sls:sls /var/lib/sls && \
    find /usr/lib/python3.* -type d -name __pycache__ -prune -exec rm -rf {} + && \
    rm -rf /usr/share/man /usr/share/doc /usr/share/info /var/cache/apk/*

# Only sls + srtla_rec - the other five binaries in the upstream image
# (slc, srt-ffplay, srt-file-transmit, srt-live-transmit, srt-tunnel) aren't
# needed here.
COPY --from=sls /usr/local/bin/sls /usr/local/bin/srtla_rec /usr/local/bin/
COPY --from=sls /usr/local/lib/libsrt.so.1.5 /usr/local/lib/libsrt.so.1.5.4 /usr/local/lib/
COPY --from=sls /usr/lib/libspdlog.so.1.17 /usr/lib/libspdlog.so.1.17.0 /usr/lib/
COPY --from=sls /usr/lib/libfmt.so.11 /usr/lib/libfmt.so.11.2.0 /usr/lib/
COPY --from=sls /etc/sls/sls.conf /etc/sls/sls.conf.upstream-default

COPY conf/sls.conf          /etc/sls/sls.conf
COPY conf/nginx.conf        /etc/nginx/nginx.conf
COPY conf/supervisord.conf  /etc/supervisord.conf
COPY conf/entrypoint.sh     /entrypoint.sh
COPY conf/apikey.py         /usr/local/bin/apikey.py
COPY conf/reload-watcher.sh /usr/local/bin/reload-watcher.sh
COPY conf/push-to-rtmp.sh   /usr/local/bin/push-to-rtmp.sh
RUN chmod +x /entrypoint.sh /usr/local/bin/reload-watcher.sh /usr/local/bin/push-to-rtmp.sh && \
    printf '' > /etc/nginx/restream.d/targets.conf

# GPL-3.0 obligation carried over from the upstream sls/srtla_rec binaries.
COPY LICENSE THIRD-PARTY.md /usr/share/licenses/ingest/

ENV RTMP_APP="live" \
    RESTREAM_APP="restream"

EXPOSE 1935/tcp 4001/udp 5000/udp 6969/tcp
# 4000/udp (SRT playback) and 8080/tcp (sls API/stats) are intentionally not
# published by compose - only OBS and the dashboard use them, both inside
# the compose network.

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/health >/dev/null 2>&1 || exit 1

CMD ["/entrypoint.sh"]
