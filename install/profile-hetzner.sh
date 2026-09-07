#!/usr/bin/env bash
# Host profile: Hetzner dedicated server (amd64, Intel iGPU).
#
# Hetzner's installimage deliberately blocks the integrated GPU twice, and
# neither block is mentioned where you would look for it. A server that shows
# the HD 630 in `lspci` can still have no /dev/dri at all, which reads exactly
# like a BIOS problem and sends people to support for nothing.
#
# Contract with install.sh: define host_prepare() and host_verify(), and set
# REBOOT_REQUIRED=1 if the changes only take effect after a restart.

GRUB_DROPIN=/etc/default/grub.d/hetzner.cfg
BLACKLIST=/etc/modprobe.d/blacklist-hetzner.conf
MODULES_CONF=/etc/modules-load.d/exnfachstreamen.conf

host_prepare() {
    step "Host profile: Hetzner dedicated"

    if [ "$(uname -m)" != "x86_64" ]; then
        die "this profile is for amd64; use --profile generic"
    fi

    # ---- 1. nomodeset --------------------------------------------------
    # With nomodeset the kernel never lets a KMS driver probe. i915 can be
    # loaded and appear in lsmod while never binding to the card, so
    # /dev/dri stays missing and dmesg says nothing at all about i915.
    # Match only inside the quoted parameter value. A plain grep for the word
    # also hits an explanatory comment that mentions it - including the one
    # this installer leaves behind - so a second run would "find" nomodeset
    # again and demand a pointless reboot.
    local nomodeset_re='^[[:space:]]*GRUB_CMDLINE_[A-Z_]*="[^"]*\bnomodeset\b'
    if [ -f "$GRUB_DROPIN" ] && grep -qE "$nomodeset_re" "$GRUB_DROPIN"; then
        info "nomodeset set in $GRUB_DROPIN (blocks the iGPU)"
        backup_file "$GRUB_DROPIN"
        # Loop until no occurrence is left, and never reach past the closing
        # quote, so trailing comments stay untouched.
        sed -i -E ':a; s/^([[:space:]]*GRUB_CMDLINE_[A-Z_]*="[^"]*)[[:space:]]*\bnomodeset\b[[:space:]]*/\1/; ta' "$GRUB_DROPIN"
        # Tidy the space the removal can leave before the closing quote.
        sed -i -E 's/^([[:space:]]*GRUB_CMDLINE_[A-Z_]*="[^"]*[^"[:space:]])[[:space:]]+"/\1"/' "$GRUB_DROPIN"
        grep -qE "$nomodeset_re" "$GRUB_DROPIN" && die "could not remove nomodeset from $GRUB_DROPIN"
        ok "removed nomodeset (explanation appended as a comment)"
        NEED_GRUB_UPDATE=1
    elif grep -qw 'nomodeset' /proc/cmdline; then
        warn "nomodeset is on the running kernel command line but not in"
        warn "$GRUB_DROPIN - check /etc/default/grub and grub.d/ by hand"
    else
        skip "nomodeset already gone"
    fi

    # ---- 2. module blacklist -------------------------------------------
    # installimage blacklists i915 and drm as "buggy kernel modules".
    if [ -f "$BLACKLIST" ] && grep -qE '^blacklist (i915|i915_bdw|drm)$' "$BLACKLIST"; then
        info "i915/drm are blacklisted in $BLACKLIST"
        backup_file "$BLACKLIST"
        sed -i -E 's/^blacklist (i915|i915_bdw|drm)$/#blacklist \1  # exnfachstreamen: VA-API encode/' "$BLACKLIST"
        ok "un-blacklisted i915, i915_bdw, drm"
    else
        skip "i915/drm not blacklisted"
    fi

    # ---- 3. regenerate grub --------------------------------------------
    if [ "${NEED_GRUB_UPDATE:-0}" = 1 ]; then
        if have update-grub; then update-grub >/dev/null 2>&1
        else grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1; fi
        ok "regenerated grub config"
        # The recovery entries keep nomodeset on purpose; only the normal
        # boot entries matter here.
        if grep -E '^\s+linux\s' /boot/grub/grub.cfg | grep -v recovery | grep -q nomodeset; then
            die "nomodeset still on a normal boot entry - stopping before a pointless reboot"
        fi
        REBOOT_REQUIRED=1
    fi

    # ---- 4. modules at boot --------------------------------------------
    # uinput is what docker-compose.prod.yml maps into the OBS container for
    # Sunshine's virtual input. It is built into some Ubuntu kernels, in which
    # case modprobe is a harmless no-op and /dev/uinput exists anyway.
    printf 'i915\nuinput\n' > "$MODULES_CONF"
    modprobe i915 2>/dev/null || true
    modprobe uinput 2>/dev/null || true
    ok "module list written to $MODULES_CONF"

    if [ "${REBOOT_REQUIRED:-0}" = 1 ]; then
        warn "a reboot is needed before the iGPU appears"
    fi
}

host_verify() {
    step "Verifying the iGPU"

    if ! lspci -nn 2>/dev/null | grep -qiE 'vga|display'; then
        warn "no VGA/display device in lspci - the iGPU is off in BIOS."
        warn "That one really does need a Hetzner support ticket or a KVM"
        warn "console; it cannot be fixed over SSH."
        return 1
    fi
    ok "GPU visible: $(lspci -nn | grep -iE 'vga|display' | head -1 | cut -d: -f3- | sed 's/^ //')"

    if [ ! -e /dev/dri/renderD128 ]; then
        if [ "${REBOOT_REQUIRED:-0}" = 1 ]; then
            warn "/dev/dri/renderD128 missing - expected, reboot pending"
        else
            warn "/dev/dri/renderD128 missing even though nothing is pending."
            warn "Check: dmesg | grep -i i915"
        fi
        return 1
    fi
    ok "render node present: /dev/dri/renderD128"

    if lspci -k -s 00:02.0 2>/dev/null | grep -q 'Kernel driver in use: i915'; then
        ok "i915 bound to the device"
    else
        warn "i915 is not bound - the module may be loaded but not probing"
        return 1
    fi

    # vainfo without --display drm picks X11 first and dies with a misleading
    # "vaGetDriverNames() failed" even when VA-API is perfectly fine.
    if have vainfo; then
        if vainfo --display drm --device /dev/dri/renderD128 2>/dev/null \
             | grep -qE 'VAProfileH264(High|Main).*VAEntrypointEncSlice'; then
            ok "VA-API H.264 hardware encode available"
        else
            warn "no H.264 encode entrypoint reported by vainfo"
            warn "The stack still works - OBS falls back to x264 in software,"
            warn "which an i7-7700 handles fine. Skip the igpu overlay then."
            return 1
        fi
    else
        skip "vainfo not installed on the host; will be checked in the container"
    fi
    return 0
}
