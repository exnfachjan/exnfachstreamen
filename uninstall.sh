#!/usr/bin/env bash
# exnfachstreamen uninstaller.
#
#   ./uninstall.sh           # remove containers + images, KEEP your data
#   ./uninstall.sh --purge   # also delete volumes, data/, .env and the app user
#   ./uninstall.sh --yes     # no prompts (with --purge: no DELETE prompt)
#
# Default keeps .env, data/ (BRB media, dashboard config) and the Docker
# volumes (OBS scenes and profile, sls stream keys, Twitch targets), so
# ./install.sh brings back the exact same setup.
#
# Never touched: Docker itself, this checkout, and the Hetzner GRUB /
# blacklist edits (backups are listed at the end). Nothing on Cloudflare's
# side is removed either - the script says what to delete there.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
. install/lib.sh

ASSUME_YES=0; PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

need_root
[ -f docker-compose.yml ] || die "run this from the repository root"
APP_USER="$(env_get APP_USER)"   # read now: --purge deletes .env below

step "What will happen"
info "stop and remove every container of this stack"
info "remove the images built for it (obs, ingest, dashboard, watchdog)"
if [ "$PURGE" = 1 ]; then
    warn "DELETE the Docker volumes: OBS scenes/profile, sls keys, Twitch targets"
    warn "DELETE data/ (BRB media, dashboard config) and .env (all secrets)"
    [ -n "$APP_USER" ] && warn "DELETE the system user $APP_USER (the checkout goes to root)"
    warn "This cannot be undone."
else
    info "keep .env, data/ and the volumes (re-run ./install.sh to restore)"
fi

if [ "$ASSUME_YES" = 1 ]; then
    info "--yes given, not asking"
elif [ "$PURGE" = 1 ]; then
    reply="$(ask 'Type DELETE to confirm')"
    [ "$reply" = DELETE ] || die "aborted - nothing was changed"
else
    confirm "Continue?" n || die "aborted - nothing was changed"
fi

# ------------------------------------------------------------- containers ---
step "Containers"
if have docker && docker compose version >/dev/null 2>&1; then
    if [ "$PURGE" = 1 ]; then
        # --rmi all also drops the pulled caddy/cloudflared images.
        docker compose down --remove-orphans --volumes --rmi all
    else
        # --rmi local: only images without a custom tag, i.e. the ones built
        # here - caddy/cloudflared stay cached for a quick reinstall.
        docker compose down --remove-orphans --rmi local
    fi
    ok "stack removed"
    docker builder prune -f >/dev/null 2>&1 && ok "build cache cleared (the OBS build leaves GBs)"
else
    skip "docker not found - nothing to stop"
fi

# ------------------------------------------------------------------- data ---
if [ "$PURGE" = 1 ]; then
    step "Data"
    rm -rf data
    ok "data/ deleted"
    rm -f .env
    ok ".env deleted"
    # Only an account install.sh created - recognisable by its comment - so a
    # --user pointing at a real person's account can never get it deleted.
    if [ -n "$APP_USER" ] && [ "$(getent passwd "$APP_USER" | cut -d: -f5)" = "$APP_USER_COMMENT" ]; then
        chown -R root:root .
        userdel -r "$APP_USER" 2>/dev/null || userdel "$APP_USER"
        ok "system user $APP_USER removed"
    elif [ -n "$APP_USER" ]; then
        skip "user $APP_USER was not created by install.sh - left alone"
    fi
fi

# ------------------------------------------------------------------- host ---
step "Host"
if [ -f /etc/modules-load.d/exnfachstreamen.conf ]; then
    rm -f /etc/modules-load.d/exnfachstreamen.conf
    ok "removed /etc/modules-load.d/exnfachstreamen.conf (takes effect on reboot)"
else
    skip "no module list to remove"
fi
BACKUPS="$(ls /etc/default/grub.d/*.pre-exnfachstreamen.* /etc/modprobe.d/*.pre-exnfachstreamen.* 2>/dev/null || true)"
if [ -n "$BACKUPS" ]; then
    info "GRUB/modprobe edits were left in place (the iGPU keeps working)."
    info "Originals, if you want them back (then run update-grub):"
    printf '%s\n' "$BACKUPS" | sed 's/^/      /'
fi

# ------------------------------------------------------------- cloudflare ---
step "Left for you to do"
info "Cloudflare Zero Trust -> Networks -> Tunnels: delete the tunnel"
info "Cloudflare DNS: delete the panel CNAME and the ingest A record"
info "Docker itself is still installed (apt purge docker-ce to remove it)"
info "This folder: rm -rf $(pwd)"
