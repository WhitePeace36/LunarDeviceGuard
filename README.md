# devguard

A small service that keeps one misbehaving device from dragging the whole
system down. It works on any kernel (no linux-lunar needed) and only uses
interfaces the kernel already offers.

| Problem | How devguard sees it | What it does |
| --- | --- | --- |
| **Interrupt storm**: a device fires interrupts nonstop and one CPU does nothing else | a CPU spends ≥25 % of its time in interrupts and one IRQ line causes most of them, or one IRQ fires ≥100,000×/s | moves that IRQ to a housekeeping CPU (`/proc/irq/N/smp_affinity_list`); optionally unbinds the driver if that doesn't help |
| **PCIe error flood**: bad slot, riser, cable or power-saving bug, the link reports correctable errors constantly | AER counters in `/sys/bus/pci/devices/*/aer_dev_correctable` rise ≥5/s | turns off ASPM (L0s/L1/L1.x) on that link; if the flood continues, masks correctable errors on the device |
| **USB reconnect loop**: a device or cable keeps dropping off and coming back | ≥8 disconnects of the same device within 60 s (kernel uevents) | disables that USB port (`usbX-portY/disable`), re-enables it after 10 minutes |
| **Kernel log flood**: a driver prints hundreds of lines per second | `/dev/kmsg` rate ≥50 lines/s | reports the typical message, lowers the console loglevel so the text console stops printing synchronously |

The kernel itself only switches off interrupts that **no** driver handles
(`irq N: nobody cared`). A device whose driver handles every one of a flood of
interrupts has no limit at all, and AER rate-limiting only shortens the log,
not the work. That is the gap devguard fills.

## Install

Arch / CachyOS:

```bash
git clone https://github.com/WhitePeace36/LunarDeviceGuard.git
cd LunarDeviceGuard
makepkg -si
sudo systemctl enable --now devguard
journalctl -u devguard -f
```

Other distributions: copy `devguard` to `/usr/bin/`, `devguard.toml` to
`/etc/`, `devguard.service` to `/etc/systemd/system/`, then enable it as
above. Needs Python 3.11 or newer, nothing else.

## Observe first, then enforce

It starts in **observe** mode and only logs what it would do:

```
[observe] would move IRQ 87 (IR-PCI-MSIX-0000:0f:00.3 0-edge xhci_hcd, 0000:0f:00.3 [1022:149c xhci_hcd]) from CPU 0-15 to CPU 0 (CPU3 70% in irq, IRQ 87 at 60000/s on it)
```

Let it run for a day of normal use, check the log for anything that is not a
real problem, adjust `/etc/devguard.toml` (thresholds, `ignore` lists), then
set `mode = "enforce"` and `sudo systemctl restart devguard`.

To see what's going on right now without the service:

```bash
sudo devguard scan --seconds 60      # observe for a minute, print findings
```

## Undo

Every change is recorded in `/run/devguard/state.json` (cleared at reboot):

```bash
sudo devguard status      # what was changed and why
sudo devguard revert      # undo all of it
```

Or set `revert_on_exit = true` to undo everything when the service stops.

## What it can't do

- It cannot stop a storm on a **kernel-managed** IRQ (NVMe queues and some
  other multi-queue devices): the kernel refuses affinity changes for those.
  devguard logs that and, if you allow it in `unbind_allow`, can unbind the
  driver instead.
- It cannot fix a driver that hangs (e.g. GPU timeouts) or a device that
  stops answering; those need the driver's own timeouts/reset.
- Disabling a USB port, unbinding a driver or masking AER errors hides a
  hardware problem; it does not fix it. The log tells you which device to
  replace, reseat or move to another port/slot.

## Tests

`python3 tests/test_devguard.py` runs devguard against fake `/proc` and `/sys`
trees with simulated storms, error floods, reconnect loops and log floods
(43 checks, no root needed).
