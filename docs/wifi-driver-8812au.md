# WiFi capture: the RTL8812AU driver swap

## Summary

On `chasingyourtail` the WiFi capture adapter is a Realtek **RTL8812AU**. With
the kernel's built-in driver, Kismet logged a steady stream of `SOURCEERROR`
failures — the WiFi source dropping and re-opening every few minutes. The cause
was a gap in the in-kernel driver, not a hardware fault. The fix is to run the
adapter on the community **aircrack-ng `88XXau`** driver instead, patched to
build on the node's kernel. After the swap the source stays up, still hops the
full 2.4 and 5 GHz spectrum, and the errors are gone.

## What was happening

Kismet hops the monitor interface across every channel a few times a second.
The in-kernel driver (`rtw88_8812au`) has **incomplete 5 GHz transmit-power
calibration tables** for this chip. Whenever a hop landed on a 5 GHz channel it
had no table entry for, the kernel raised a warning, the channel change failed,
and the capture helper process died — which Kismet reports as
`SOURCEERROR … IPC connection closed`, then restarts the source five seconds
later. Over a session that was hundreds of failures. Every failure traced back
through the kernel to the same function setting the monitor channel, and it
only ever happened on 5 GHz — 2.4 GHz was always fine.

Narrowing Kismet to a safe subset of channels would have stopped the errors but
would also have blinded the sensor to whole swaths of the spectrum, so that was
rejected. The right fix was to replace the driver and keep every channel.

## Why the swap needed patching

The RTL8812AU is much better served by the out-of-tree aircrack-ng driver,
which has complete 2.4 and 5 GHz (including DFS) tables and is built for
monitor-mode hopping. But that driver's last release predates several kernel
changes, so on a recent kernel (6.15–6.18) it does not build as-shipped. Three
fixes were needed, all captured in `deploy/wifi-8812au/`:

1. **Build system.** Recent kernels ignore the driver's deprecated
   `EXTRA_CFLAGS`; converting those to `ccflags-y` restores its include paths
   and configuration defines.
2. **Timer API.** Kernel 6.15/6.16 removed the legacy timer calls the driver
   uses (`del_timer`, `del_timer_sync`, `from_timer`). A small version-guarded
   compatibility shim maps them to the current names.
3. **cfg80211 signatures.** Kernel 6.18 added a radio-index parameter to three
   wireless callbacks (`set_wiphy_params`, `set_tx_power`, `get_tx_power`). The
   driver's callbacks get the extra parameter under a version guard.

## One trade-off after the swap

The aircrack-ng driver is happy on all frequencies, but it raises a harmless
kernel warning every time it's asked to set a **40/80 MHz-wide** channel — which
during hopping happens constantly and floods the kernel log. It never drops the
source; it's only log noise. The remedy is to have Kismet hop at **20 MHz width
only** (`kismet-88XXau-20mhz.conf`). That still visits **every frequency** —
2.4 and 5 GHz including DFS — so there is no loss of coverage for passive
capture; it just listens at 20 MHz instead of 40/80. With that in place the log
is quiet.

## Installing / reproducing

```
sudo deploy/wifi-8812au/install-driver.sh
# then append kismet-88XXau-20mhz.conf's two lines to /etc/kismet/kismet_site.conf
sudo reboot
```

The installer fetches a pinned driver commit, applies the patches, builds and
installs it via **DKMS** (so it rebuilds automatically on future kernel
updates), and blacklists the in-tree driver. The USB adapter comes back up as
`wlan1` on the `rtl88XXau` driver — the interface name is unchanged, so no
Kismet or Passive Vigilance settings need editing.

Verify after reboot:

```
basename $(readlink -f /sys/class/net/wlan1/device/driver)   # -> rtl88XXau
lsmod | grep rtw88_8812au                                    # -> (nothing)
```

## Notes

- Only the RTL8812AU (`0bda:8812`) is affected. The Bluetooth radio and the
  SDR are separate devices and are untouched.
- This was validated live and across a reboot: full 2.4 + 5 GHz hopping at
  20 MHz, zero steady-state source errors, zero kernel warnings, capture
  healthy. (A couple of source re-opens can occur in the first ~2 minutes after
  a cold boot while Kismet negotiates its channel list; these self-heal.)
- The durable hardware alternative, if this ever needs revisiting, is a
  MediaTek MT7612U adapter on the in-tree `mt76` driver — first-class
  monitor-mode support with no out-of-tree build to maintain.
