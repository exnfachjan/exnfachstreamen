#!/usr/bin/env bash
# exnfachstreamen installer.
#
# Takes a fresh server to a running stack: host quirks, Docker, secrets, DNS
# sanity, build, start. Safe to run more than once - every step checks the
# current state first and skips what is already done, so the normal way to
# recover from an interruption is to run it again.
#
# Run it as root. It creates a system user for the stack (default
# "exnfachstreamen") that owns the checkout, .env and data/.
#
#   ./install.sh                    # detect the host, ask what it needs
#   ./install.sh --profile hetzner  # force a profile
#   ./install.sh --user NAME        # name of the app user
#   ./install.sh --yes              # accept every default, no prompts
#   ./install.sh --skip-build       # configure but do not build or start
#
# Later: ./update.sh to update, ./uninstall.sh to remove.
#
# Host-specific work lives in install/profile-*.sh. Everything else here is
# the same on any machine.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
. install/lib.sh

PROFILE=""; ASSUME_YES=0; SKIP_BUILD=0; REBOOT_REQUIRED=0; APP_USER=""

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2-}"; shift 2 ;;
        --profile=*) PROFILE="${1#*=}"; shift ;;
        --user) APP_USER="${2-}"; shift 2 ;;
        --user=*) APP_USER="${1#*=}"; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

need_root
[ -f docker-compose.yml ] || die "run this from the repository root"

# ---------------------------------------------------------------- profile ---
detect_profile() {
    # Hetzner's installimage leaves two unmistakable fingerprints. Detecting
    # them beats asking, and asking beats guessing wrong on someone else's box.
    if [ -f /etc/default/grub.d/hetzner.cfg ] || [ -f /etc/modprobe.d/blacklist-hetzner.conf ]; then
        echo hetzner; return
    fi
    echo generic
}

if [ -z "$PROFILE" ]; then
    PROFILE="$(detect_profile)"
    step "Detected host profile: $PROFILE"
    [ "$PROFILE" = hetzner ] && info "(found Hetzner installimage fingerprints in /etc)"
    confirm "Use the '$PROFILE' profile?" y || PROFILE="$(ask 'Profile (hetzner/generic)' generic)"
fi
[ -f "install/profile-$PROFILE.sh" ] || die "no such profile: $PROFILE"
# shellcheck source=/dev/null  # the whole point is that the profile varies
. "install/profile-$PROFILE.sh"

# ------------------------------------------------------------- preflight ---
step "Preflight"
. /etc/os-release 2>/dev/null || true
info "OS:     ${PRETTY_NAME:-unknown}"
info "Kernel: $(uname -r)  ($(uname -m))"
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
info "Free on /: ${FREE_GB} GB"
# The OBS source build plus CEF plus Docker's build cache is the constraint,
# not the running stack.
[ "${FREE_GB:-0}" -ge 25 ] || warn "under 25 GB free - the OBS build alone needs well over 10"

# ----------------------------------------------------------- host profile ---
host_prepare

if [ "$REBOOT_REQUIRED" = 1 ]; then
    step "Reboot required"
    info "The GPU changes only take effect after a restart."
    info "Reboot, then run this script again - it will pick up where it stopped."
    if confirm "Reboot now?" n; then
        info "rebooting..."; sleep 2; systemctl reboot; exit 0
    fi
    exit 0
fi

# ---------------------------------------------------------------- docker ---
step "Docker"
if have docker && docker compose version >/dev/null 2>&1; then
    skip "already installed: $(docker --version | cut -d, -f1)"
else
    info "installing from get.docker.com"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh >/dev/null
    rm -f /tmp/get-docker.sh
    ok "installed: $(docker --version | cut -d, -f1)"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# ------------------------------------------------------------------- env ---
# --------------------------------------------------------------- app user ---
step "App user"
# A re-run keeps the user from the first run unless --user says otherwise.
[ -n "$APP_USER" ] || APP_USER="$(env_get APP_USER)"
[ -n "$APP_USER" ] || APP_USER="$APP_USER_DEFAULT"
ensure_app_user "$APP_USER"

step "Configuration (.env)"
if [ -f .env ]; then
    skip ".env exists - keeping it (delete it to start over)"
else
    DOMAIN="$(ask 'Panel hostname, served through the Cloudflare tunnel (e.g. stream.example.com)')"
    [ -n "$DOMAIN" ] || die "a panel hostname is required"

    DASH_PASS="$(ask 'Dashboard password (blank = generate one)')"
    if [ -z "$DASH_PASS" ]; then
        DASH_PASS="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
        info "generated dashboard password: $DASH_PASS"
    fi

    info "hashing the password with caddy..."
    # Fed over stdin, not as --plaintext: process arguments are world-readable
    # through /proc/<pid>/cmdline, so any local account could read the password
    # out of `ps` for as long as the container runs. The trailing newline is
    # stripped by caddy. --algorithm is explicit because the dashboard verifies
    # with bcrypt.checkpw(), and caddy's help already nudges towards argon2id.
    HASH="$(printf '%s\n' "$DASH_PASS" \
        | docker run --rm -i caddy caddy hash-password --algorithm bcrypt)"
    [ ${#HASH} -eq 60 ] || die "unexpected bcrypt hash length ${#HASH}"
    # compose interpolates .env values, and a bare $ is silently swallowed as
    # an undefined variable - which corrupts a bcrypt hash beyond repair.
    HASH_ESCAPED="${HASH//\$/\$\$}"

    sed -e "s|^PUBLISH_KEY=.*|PUBLISH_KEY=$(openssl rand -hex 16)|" \
        -e "s|^RESTREAM_KEY=.*|RESTREAM_KEY=$(openssl rand -hex 16)|" \
        -e "s|^OBS_WS_PASSWORD=.*|OBS_WS_PASSWORD=$(openssl rand -hex 16)|" \
        -e "s|^DASH_PASS_HASH=.*|DASH_PASS_HASH=${HASH_ESCAPED}|" \
        -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
        .env.example > .env
    chmod 600 .env
    ok ".env written (mode 600, git-ignored)"
    printf '    %sDashboard login: admin / %s%s\n' "$BLD" "$DASH_PASS" "$RST"
    printf '    %sWrite that down now - it is not stored anywhere else.%s\n' "$YLW" "$RST"
fi
env_set APP_USER "$APP_USER"
ensure_tunnel_env
DOMAIN="$(env_get DOMAIN)"
INGEST_HOST="$(env_get INGEST_HOST)"

# ------------------------------------------------------------ resolution ---
# Canvas size lives in two files and they have to agree. If they drift, the
# encoder either downscales every frame for nothing or the watchdog stretches
# sources past the edges of the canvas.
step "Canvas resolution"
info "current seed: $(current_resolution)p"
apply_resolution "$(ask 'Resolution - 720 (30 fps, 4500k) or 1080 (60 fps, 7000k)' "${RES_DEFAULT:-1080}")"
info "(the seed only applies to a fresh obs_config volume; an existing"
info " install keeps whatever its OBS profile already has)"

# ------------------------------------------------------------------- gpu ---
if host_verify; then
    IGPU=1; ok "hardware encoding will be available"
else
    IGPU=0; warn "continuing without the iGPU overlay (software x264)"
fi

COMPOSE_FILES="docker-compose.yml:docker-compose.prod.yml"
[ "$IGPU" = 1 ] && COMPOSE_FILES="$COMPOSE_FILES:docker-compose.igpu.yml"
if grep -q '^COMPOSE_FILE=' .env; then
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$COMPOSE_FILES|" .env
else
    printf '\n# Pinned by install.sh so a plain "docker compose" picks up every file.\nCOMPOSE_FILE=%s\n' "$COMPOSE_FILES" >> .env
fi
ok "COMPOSE_FILE=$COMPOSE_FILES"

# ------------------------------------------------------------------- dns ---
# Only the ingest hostname is checked: the panel hostname is a CNAME that the
# tunnel creates itself and that resolves to Cloudflare, never to this box.
step "DNS (ingest)"
MYIP="$(public_ipv4)"
info "this server: ${MYIP:-unknown}"
if have dig; then RESOLVED="$(dig +short A "$INGEST_HOST" @1.1.1.1 2>/dev/null | tail -1)"
else RESOLVED="$(getent ahostsv4 "$INGEST_HOST" 2>/dev/null | awk '{print $1; exit}')"; fi
info "$INGEST_HOST resolves to: ${RESOLVED:-nothing}"

if [ -z "$RESOLVED" ]; then
    warn "no A record yet - phones will not find the ingest until there is one."
elif [ "$RESOLVED" != "$MYIP" ]; then
    warn "the A record does not point at this server."
    warn "On Cloudflare it must be DNS-only (grey cloud): the orange-cloud"
    warn "proxy carries no UDP, which kills SRT and SRTLA."
else
    ok "A record points here"
fi

# ----------------------------------------------------------------- build ---
chown_app "$APP_USER"

if [ "$SKIP_BUILD" = 1 ]; then
    step "Done (build skipped)"
    info "Run: docker compose build && docker compose up -d"
    exit 0
fi

step "Build"
info "OBS is compiled from source and CEF is a ~300 MB download."
info "Expect roughly 10-25 minutes on a first run, less on a rebuild."
docker compose build

step "Start"
docker compose up -d --remove-orphans
restart_ingest_after_up

chown_app "$APP_USER"

step "Done"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
printf '\n    Dashboard: %shttps://%s/%s\n' "$BLD" "$DOMAIN" "$RST"
printf '    Ingest:    %s%s%s (UDP 5000/4001, TCP 1935)\n' "$BLD" "$INGEST_HOST" "$RST"
info "Panel unreachable? The tunnel needs a Public Hostname in Zero Trust:"
info "  $DOMAIN -> HTTP -> caddy:80     Check: docker compose logs cloudflared"
