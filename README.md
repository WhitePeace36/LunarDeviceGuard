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

## Known causes of stutters: `devguard check`

```bash
sudo devguard check
```

One pass over things that are known to make a whole system stutter, as a
checklist (`!` = worth a look):

- CPU, board and BIOS version: many firmware stalls are fixed by BIOS updates
- **AMD firmware TPM (fTPM)**: on many Ryzen boards with older AGESA it stalls
  the whole system; flagged when an AMD CPU uses the `tpm_crb` TPM, and
  especially when the kernel reads randomness from it (`tpm-rng-0`)
- clocksource: anything other than `tsc` makes timekeeping slow
- SMIs (firmware interrupts) counted for 2 s (AMD: PMU event `ls_smi_rx`;
  Intel: MSR 0x34, needs `modprobe msr`; not available in VMs)
- the kernel log since boot: clocksource problems, long interrupt handlers,
  lockups, switched-off IRQs, PCIe/hardware errors, USB resets and
  disconnects, disk errors, GPU timeouts, TPM errors, thermal throttling

## Short freezes (tens of milliseconds)

The guards above react to problems that last seconds. For short freezes
(the picture stops, keystrokes get lost, then everything jumps ahead) use:

```bash
sudo devguard freeze                 # until Ctrl+C
sudo devguard freeze --threshold 20  # report stalls > 20 ms (default 30)
```

A normal-priority loop checks every 5 ms whether it was held up. After every
freeze it prints how long it was and what the kernel recorded in that moment:

```
21:06:47.910  FREEZE 407 ms (this watcher was held up)
   -> MEMORY: tasks stalled in reclaim/compaction (allocstall/compact_stall); ...
   psi (ms stalled in this window): memory some 380  memory full 350  io some 0 ...
   vmstat: allocstall +4, compact_stall +2
   SMIs (firmware interrupts): 0
   busiest interrupts: LOC 18000/s, 87 2500/s (... xhci_hcd), ...
   kernel: <any kernel message from that moment>
```

| Verdict | Meaning |
| --- | --- |
| MEMORY | tasks were stuck in memory reclaim/compaction |
| INTERRUPTS | CPUs were busy in interrupt handlers (busiest line shown) |
| I/O | tasks waited for the disk, e.g. for pages read back from swap |
| CPU | runnable tasks waited for a CPU |
| FIRMWARE | SMIs were counted during the freeze: the firmware stopped the system |
| UNACCOUNTED | the kernel recorded no pressure at all: typical for firmware (SMI, if they can't be counted) or a driver keeping interrupts off; lost keystrokes point the same way |

If you see a freeze but devguard prints nothing, press Enter: it then shows
the last 3 seconds. No stall of the watcher means the CPU side ran normally
and the freeze was in the GPU/display path.

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
(60 checks, no root needed).
