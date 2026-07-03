#!/usr/bin/env python3
"""Patch the aircrack-ng rtl8812au source so it builds on Linux 6.15-6.18.

The aircrack-ng 88XXau driver (5.6.4.2, 2023) predates three kernel changes
that break the build on 6.15+. This applies the two C-source fixes; the
Makefile `EXTRA_CFLAGS -> ccflags-y` conversion is done in install-driver.sh
(a plain sed over many files).

  1. Legacy timer API removed (6.15/6.16): del_timer/del_timer_sync ->
     timer_delete/timer_delete_sync, from_timer -> timer_container_of.
     Handled with a version-guarded compat shim in osdep_service_linux.h.

  2. cfg80211 gained an `int radio_idx` parameter on set_wiphy_params,
     set_tx_power and get_tx_power (6.18 multi-radio wiphy series). The
     driver's three callbacks get the extra parameter, version-guarded.

Usage: apply-linux-patches.py <driver-source-dir>
Idempotent: re-running is a no-op (anchors are already rewritten).
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()

# ---- 1. timer compat shim -------------------------------------------------
osdep = root / "include/osdep_service_linux.h"
t = osdep.read_text()
shim = """#include <linux/version.h>

/* --- PV compat shim: kernel 6.15/6.16 removed the legacy timer API. --- */
#ifndef PV_TIMER_COMPAT_H
#define PV_TIMER_COMPAT_H
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 16, 0)
#ifndef from_timer
#define from_timer(var, callback_timer, timer_fieldname) \\
\ttimer_container_of(var, callback_timer, timer_fieldname)
#endif
#endif
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
#ifndef del_timer
#define del_timer(t) timer_delete(t)
#endif
#ifndef del_timer_sync
#define del_timer_sync(t) timer_delete_sync(t)
#endif
#endif
#endif /* PV_TIMER_COMPAT_H */
"""
if "PV_TIMER_COMPAT_H" not in t:
    anchor = "#include <linux/version.h>\n"
    assert t.count(anchor) >= 1, "version.h include not found in osdep_service_linux.h"
    osdep.write_text(t.replace(anchor, shim, 1))
    print("timer shim: inserted")
else:
    print("timer shim: already present (skipped)")

# ---- 2. cfg80211 radio_idx (6.18) -----------------------------------------
cfg = root / "os_dep/linux/ioctl_cfg80211.c"
c = cfg.read_text()
radio = ("#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 18, 0))\n"
         "\tint radio_idx,\n"
         "#endif\n")

if "KERNEL_VERSION(6, 18, 0)" not in c:
    # set_wiphy_params: single-line signature -> guarded multiline
    swp_old = "static int cfg80211_rtw_set_wiphy_params(struct wiphy *wiphy, u32 changed)\n"
    swp_new = ("static int cfg80211_rtw_set_wiphy_params(struct wiphy *wiphy,\n"
               + radio + "\tu32 changed)\n")
    assert c.count(swp_old) == 1, f"set_wiphy_params anchor count={c.count(swp_old)}"
    c = c.replace(swp_old, swp_new, 1)

    # set_tx_power / get_tx_power: insert radio_idx after the wdev block
    for fn in ("cfg80211_rtw_set_txpower", "cfg80211_rtw_get_txpower"):
        old = ("static int %s(struct wiphy *wiphy,\n"
               "#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))\n"
               "\tstruct wireless_dev *wdev,\n"
               "#endif\n" % fn)
        assert c.count(old) == 1, f"{fn} anchor count={c.count(old)}"
        c = c.replace(old, old + radio, 1)

    cfg.write_text(c)
    print("cfg80211 radio_idx: set_wiphy_params, set_tx_power, get_tx_power patched")
else:
    print("cfg80211 radio_idx: already present (skipped)")
