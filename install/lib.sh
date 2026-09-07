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
