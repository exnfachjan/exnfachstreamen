#!/usr/bin/env bash
# Host profile: anything that is not a Hetzner dedicated box.
#
# Touches no boot configuration and no module blacklists, because on an
# unknown host those are as likely to be deliberate as accidental. It only
# reports what it finds, so the operator can decide whether to use the iGPU
# overlay at all.
#
# Contract with install.sh: define host_prepare() and host_verify().

MODULES_CONF=/etc/modules-load.d/exnfachstreamen.conf

host_prepare() {
    step "Host profile: generic"

    info "No platform-specific changes on this profile."
    info "Nothing in GRUB or modprobe.d is touched."

    printf 'uinput\n' > "$MODULES_CONF"
    modprobe uinput 2>/dev/null || true
    if [ -e /dev/uinput ]; then
        ok "uinput available (needed by docker-compose.prod.yml)"
    else
        warn "no /dev/uinput - the obs container will fail to start with"
        warn "docker-compose.prod.yml layered on. Either provide the module or"
        warn "drop that file's devices: entry."
    fi

    # Only load i915 where there is actually an Intel GPU to bind it to.
    # lspci prints the class before the vendor ("VGA compatible controller:
    # Intel Corporation HD Graphics 630"), so match the two independently
    # rather than assuming an order.
    if lspci -nn 2>/dev/null | grep -iE 'vga|display' | grep -qi intel; then
        printf 'i915\nuinput\n' > "$MODULES_CONF"
        modprobe i915 2>/dev/null || true
        ok "Intel graphics detected, i915 added to $MODULES_CONF"
    fi
}

host_verify() {
    step "Checking for a usable GPU"

    if [ ! -e /dev/dri/renderD128 ]; then
        info "No /dev/dri/renderD128 on this host."
        info "That is not a problem: the stack runs fine on software x264."
        info "The installer will leave docker-compose.igpu.yml out."
        return 1
    fi
    ok "render node present: /dev/dri/renderD128"

    if have vainfo && vainfo --display drm --device /dev/dri/renderD128 2>/dev/null \
         | grep -qE 'VAProfileH264(High|Main).*VAEntrypointEncSlice'; then
        ok "VA-API H.264 hardware encode available"
        return 0
    fi

    # A render node with no verified encode path is not worth enabling the
    # overlay for: with the overlay and no usable device the obs container
    # refuses to start, which is a worse failure than staying on x264.
    warn "render node exists but no H.264 encode entrypoint was confirmed"
    return 1
}
