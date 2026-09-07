#!/usr/bin/env bash
# exnfachstreamen installer.
#
# Takes a fresh server to a running stack: host quirks, Docker, secrets, DNS
# sanity, build, start. Safe to run more than once - every step checks the
# current state first and skips what is already done, so the normal way to
# recover from an interruption is to run it again.
#
#   sudo ./install.sh                    # detect the host, ask what it needs
#   sudo ./install.sh --profile hetzner  # force a profile
#   sudo ./install.sh --yes              # accept every default, no prompts
#   sudo ./install.sh --skip-build       # configure but do not build or start
#
# Host-specific work lives in install/profile-*.sh. Everything else here is
# the same on any machine.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
. install/lib.sh

PROFILE=""; ASSUME_YES=0; SKIP_BUILD=0; REBOOT_REQUIRED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2-}"; shift 2 ;;
        --profile=*) PROFILE="${1#*=}"; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
step "Configuration (.env)"
if [ -f .env ]; then
    skip ".env exists - keeping it (delete it to start over)"
else
    DOMAIN="$(ask 'Domain that phones and viewers will use (e.g. stream.example.com)')"
    [ -n "$DOMAIN" ] || die "DOMAIN is required; Caddy refuses to start without one"

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
DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2-)"

# ------------------------------------------------------------ resolution ---
# Canvas size lives in two files and they have to agree. If they drift, the
# encoder either downscales every frame for nothing or the watchdog stretches
# sources past the edges of the canvas.
step "Canvas resolution"
CUR_W=$(sed -n 's/^BaseCX=\([0-9]*\).*/\1/p' obs/config/profile-basic.ini | head -1)
info "current seed: ${CUR_W:-?}px wide"
RES="$(ask 'Resolution - 720 or 1080' "${RES_DEFAULT:-720}")"
case "$RES" in
    720)  CW=1280; CH=720;  VB=4500 ;;
    1080) CW=1920; CH=1080; VB=6000 ;;
    *) die "resolution must be 720 or 1080" ;;
esac
sed -i -E "s/^BaseCX=.*/BaseCX=$CW/;   s/^BaseCY=.*/BaseCY=$CH/;
           s/^OutputCX=.*/OutputCX=$CW/; s/^OutputCY=.*/OutputCY=$CH/;
           s/^VBitrate=.*/VBitrate=$VB/" obs/config/profile-basic.ini
sed -i -E "s/^CANVAS_W, CANVAS_H = .*/CANVAS_W, CANVAS_H = $CW, $CH/" watchdog/watchdog.py
ok "${CW}x${CH} @ ${VB} kbps, written to the OBS seed and the watchdog"
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
step "DNS"
MYIP="$(public_ipv4)"
info "this server: ${MYIP:-unknown}"
if have dig; then RESOLVED="$(dig +short A "$DOMAIN" @1.1.1.1 2>/dev/null | tail -1)"
else RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"; fi
info "$DOMAIN resolves to: ${RESOLVED:-nothing}"

if [ -z "$RESOLVED" ]; then
    warn "no A record. Caddy cannot get a certificate without one."
    confirm "Continue anyway?" n || die "set the A record, then re-run"
elif [ "$RESOLVED" != "$MYIP" ]; then
    warn "the A record does not point at this server."
    warn "If the domain is on Cloudflare, set it to DNS-only (grey cloud):"
    warn "the orange-cloud proxy carries no UDP, which kills SRT and SRTLA."
    confirm "Continue anyway?" n || die "fix DNS first - failed ACME attempts burn Let's Encrypt rate limits"
else
    ok "A record points here"
fi

# ----------------------------------------------------------------- build ---
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
docker compose up -d
# nginx resolves the dashboard hostname for its on_publish callback once, at
# config load, and caches the container IP. If the dashboard was recreated
# after ingest, RTMP publishes get rejected with nothing in any log to show it.
sleep 5
docker compose restart ingest >/dev/null

step "Done"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
printf '\n    Dashboard: %shttps://%s/%s\n' "$BLD" "$DOMAIN" "$RST"
info "The first load can take a few seconds while Caddy gets its certificate."
info "Watch it with: docker compose logs -f caddy"
