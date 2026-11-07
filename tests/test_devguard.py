#!/usr/bin/env python3
"""Fixture tests for devguard: fake /proc and /sys trees with simulated
faulty hardware. Run: python3 tests/test_devguard.py"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="devguard-fx-")
os.environ["DEVGUARD_ROOT"] = ROOT
os.environ["DEVGUARD_STATE"] = os.path.join(ROOT, "run/devguard/state.json")

loader = importlib.machinery.SourceFileLoader("devguard", os.path.join(HERE, "..", "devguard"))
spec = importlib.util.spec_from_loader("devguard", loader)
dg = importlib.util.module_from_spec(spec)
loader.exec_module(dg)

FAILS = 0
NCPU = 4


def check(cond, what):
    global FAILS
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILS += 1


def put(p, text, mode=None):
    full = ROOT + p
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)


def get(p):
    with open(ROOT + p) as f:
        return f.read().strip()


def captured(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


# ---------------------------------------------------------------- fixtures --

class Machine:
    """Cumulative counters for /proc/interrupts and /proc/stat."""

    def __init__(self):
        self.irqs = {  # irq -> (name, per-cpu counts)
            87: ("IR-PCI-MSIX-0000:0f:00.3 0-edge xhci_hcd", [0] * NCPU),
            78: ("IR-PCI-MSIX-0000:08:00.3 0-edge xhci_hcd", [0] * NCPU),
            120: ("IR-PCI-MSIX-0000:05:00.0 3-edge nvme0q3", [0] * NCPU),
        }
        self.loc = [0] * NCPU
        self.stat = [[0] * 10 for _ in range(NCPU)]  # jiffies per field

    def tick(self, seconds, irq_rates, irq_share):
        """irq_rates: {irq: {cpu: per-second}}; irq_share: {cpu: fraction}"""
        for irq, per in irq_rates.items():
            for cpu, r in per.items():
                self.irqs[irq][1][cpu] += int(r * seconds)
        for cpu in range(NCPU):
            self.loc[cpu] += 1000 * seconds
            j = 100 * seconds  # USER_HZ
            share = irq_share.get(cpu, 0.0)
            self.stat[cpu][5] += int(j * share * 0.8)   # irq
            self.stat[cpu][6] += int(j * share * 0.2)   # softirq
            self.stat[cpu][3] += j - int(j * share * 0.8) - int(j * share * 0.2)  # idle
        self.write()

    def write(self):
        lines = ["     " + "".join(f"CPU{c:<11}" for c in range(NCPU))]
        for irq in sorted(self.irqs):
            name, counts = self.irqs[irq]
            lines.append(f"{irq:>4}: " + "".join(f"{c:>10} " for c in counts) + " " + name)
        lines.append(" LOC: " + "".join(f"{c:>10} " for c in self.loc) + "  Local timer interrupts")
        put("/proc/interrupts", "\n".join(lines) + "\n")
        st = ["cpu  0 0 0 0 0 0 0 0 0 0"]
        for c in range(NCPU):
            st.append(f"cpu{c} " + " ".join(str(v) for v in self.stat[c]))
        put("/proc/stat", "\n".join(st) + "\nintr 0\n")


def make_pci(dev, vendor, device, driver, msi=(), link=None, aer=None, aer_cap=True):
    base = f"/sys/bus/pci/devices/{dev}"
    put(base + "/vendor", vendor + "\n")
    put(base + "/device", device + "\n")
    drvdir = ROOT + f"/sys/bus/pci/drivers/{driver}"
    os.makedirs(drvdir, exist_ok=True)
    put(f"/sys/bus/pci/drivers/{driver}/unbind", "")
    os.symlink(drvdir, ROOT + base + "/driver")
    for irq in msi:
        put(base + f"/msi_irqs/{irq}", "msix\n")
    if link:
        for f, v in link.items():
            put(base + f"/link/{f}", f"{v}\n")
    if aer is not None:
        set_aer(dev, aer)
    cfg = bytearray(4096)
    if aer_cap:
        # ext cap header at 0x100: id 0x0001 (AER), version 2, next 0x148
        cfg[0x100:0x104] = (0x0001 | (2 << 16) | (0x148 << 20)).to_bytes(4, "little")
        cfg[0x114:0x118] = (0x00002000).to_bytes(4, "little")  # default: advisory masked
        cfg[0x148:0x14C] = (0x000B | (1 << 16)).to_bytes(4, "little")  # some other cap, end
    else:
        cfg[0x100:0x104] = (0x000B | (1 << 16)).to_bytes(4, "little")
    with open(ROOT + base + "/config", "wb") as f:
        f.write(cfg)


def set_aer(dev, cor, nonfatal=0, fatal=0):
    base = f"/sys/bus/pci/devices/{dev}"
    put(base + "/aer_dev_correctable", f"RxErr {cor}\nBadTLP 0\nTOTAL_ERR_COR {cor}\n")
    put(base + "/aer_dev_nonfatal", f"Undefined 0\nTOTAL_ERR_NONFATAL {nonfatal}\n")
    put(base + "/aer_dev_fatal", f"Undefined 0\nTOTAL_ERR_FATAL {fatal}\n")


def cfg_dword(dev, off):
    with open(ROOT + f"/sys/bus/pci/devices/{dev}/config", "rb") as f:
        f.seek(off)
        return int.from_bytes(f.read(4), "little")


def reset_state():
    if os.path.exists(os.environ["DEVGUARD_STATE"]):
        os.remove(os.environ["DEVGUARD_STATE"])


def cfg(section, **over):
    c = dict(dg.DEFAULTS[section])
    c.update(over)
    return c


# ------------------------------------------------------------------- tests --

def test_irq():
    print("IRQ storms")
    m = Machine()
    for irq in (87, 78, 120):
        put(f"/proc/irq/{irq}/smp_affinity_list", "0-3\n")
    make_pci("0000:0f:00.3", "0x1022", "0x149c", "xhci_hcd", msi=[87])
    make_pci("0000:08:00.3", "0x1022", "0x149c", "xhci_hcd", msi=[78])
    make_pci("0000:05:00.0", "0x144d", "0xa80a", "nvme", msi=[120])

    # 1. normal desktop (the rates from the user's devwatch run): nothing
    reset_state()
    acts = dg.Actions(enforce=True)
    g = dg.IrqGuard(cfg("irq"), acts)
    out = ""
    t = 0.0
    m.write()
    for _ in range(10):
        out += captured(g.sample, t)
        m.tick(1, {87: {3: 2500}, 78: {5 % NCPU: 1070}, 120: {1: 400}}, {3: 0.01})
        t += 1
    out += captured(g.sample, t)
    check(out == "" and not g.findings, "normal desktop load: no finding")

    # 2. storm: IRQ 87 at 60k/s on CPU3, CPU3 70% in irq -> moved to CPU 0
    out = ""
    for _ in range(4):
        m.tick(1, {87: {3: 60000}, 78: {1: 1000}}, {3: 0.7})
        t += 1
        out += captured(g.sample, t)
    check(get("/proc/irq/87/smp_affinity_list") == "0", "storming IRQ moved to housekeeping CPU 0")
    check("move IRQ 87" in out and "0000:0f:00.3" in out and "xhci_hcd" in out,
          "log names IRQ, PCI address and driver")
    check(get("/proc/irq/78/smp_affinity_list") == "0-3", "other IRQs untouched")
    st = json.load(open(os.environ["DEVGUARD_STATE"]))
    check(len(st) == 1 and st[0]["old"] == "0-3", "change recorded with old value")

    # 3. storm continues on CPU0 past escalate_after: no unbind (not allowed)
    out = ""
    for _ in range(35):
        m.tick(1, {87: {0: 60000}}, {0: 0.7})
        t += 1
        out += captured(g.sample, t)
    check("continues after" in out and "unbind_allow" in out, "escalation only logs without unbind_allow")
    check(out.count("continues after") == 1, "escalation message only once")

    # 4. revert
    out = captured(acts.revert)
    check(get("/proc/irq/87/smp_affinity_list") == "0-3", "revert restores the affinity")

    # 5. escalation with unbind_allow
    reset_state()
    acts = dg.Actions(enforce=True)
    g = dg.IrqGuard(cfg("irq", unbind_allow=["0000:0f:00.3"], escalate_after=5), acts)
    out = captured(g.sample, t)
    for _ in range(12):
        m.tick(1, {87: {3: 60000}}, {3: 0.7})
        t += 1
        out += captured(g.sample, t)
    check(get("/sys/bus/pci/drivers/xhci_hcd/unbind") == "0000:0f:00.3",
          "with unbind_allow the driver gets unbound after escalate_after")
    put("/sys/bus/pci/drivers/xhci_hcd/unbind", "")
    put("/proc/irq/87/smp_affinity_list", "0-3\n")

    # 6. absolute rate rule without CPU-time accounting (share 0)
    reset_state()
    acts = dg.Actions(enforce=False)
    g = dg.IrqGuard(cfg("irq"), acts)
    out = captured(g.sample, t)
    for _ in range(4):
        m.tick(1, {120: {0: 40000, 1: 40000, 2: 40000}}, {})
        t += 1
        out += captured(g.sample, t)
    check("[observe] would move IRQ 120" in out and "120000 interrupts/s" in out,
          "rate rule fires at 120k/s; observe mode only logs")
    check(get("/proc/irq/120/smp_affinity_list") == "0-3", "observe mode changes nothing")

    # 7. ignore list
    g = dg.IrqGuard(cfg("irq", ignore=["nvme"]), dg.Actions(enforce=True))
    out = captured(g.sample, t)
    for _ in range(4):
        m.tick(1, {120: {0: 40000, 1: 40000, 2: 40000}}, {})
        t += 1
        out += captured(g.sample, t)
    check(out == "", "ignored IRQ names are skipped")

    # 8. write failure (e.g. kernel-managed IRQ) is reported, not fatal
    reset_state()
    os.remove(ROOT + "/proc/irq/87/smp_affinity_list")
    os.makedirs(ROOT + "/proc/irq/87/smp_affinity_list")
    g = dg.IrqGuard(cfg("irq"), dg.Actions(enforce=True))
    out = captured(g.sample, t)
    for _ in range(4):
        m.tick(1, {87: {3: 60000}}, {3: 0.7})
        t += 1
        out += captured(g.sample, t)
    check("failed to move IRQ 87" in out, "failing affinity write is logged")
    os.rmdir(ROOT + "/proc/irq/87/smp_affinity_list")
    put("/proc/irq/87/smp_affinity_list", "0-3\n")

    # 9. a new storm long after the previous one starts a new episode
    reset_state()
    g = dg.IrqGuard(cfg("irq", escalate_after=30), dg.Actions(enforce=True))
    out = captured(g.sample, t)
    for _ in range(4):
        m.tick(1, {87: {3: 60000}}, {3: 0.7})
        t += 1
        out += captured(g.sample, t)
    for _ in range(100):  # quiet
        m.tick(1, {}, {})
        t += 1
        out += captured(g.sample, t)
    out = ""
    for _ in range(4):
        m.tick(1, {87: {0: 60000}}, {0: 0.7})
        t += 1
        out += captured(g.sample, t)
    check("continues after" not in out and "already confined" in out,
          "new episode after a quiet period does not escalate at once")


def test_aer():
    print("PCIe AER floods")
    reset_state()
    # endpoint with ASPM link controls (e.g. a wifi card on a bad link)
    make_pci("0000:04:00.0", "0x8086", "0x2725", "iwlwifi",
             link={"l0s_aspm": 0, "l1_aspm": 1, "l1_1_aspm": 1, "l1_2_aspm": 1, "clkpm": 1},
             aer=0)
    # root port without link/ dir, child below it
    make_pci("0000:00:01.1", "0x1022", "0x1483", "pcieport", aer=0)
    make_pci("0000:01:00.0", "0x1987", "0x5018", "nvme", link={"l1_aspm": 1}, aer=0)
    os.makedirs(ROOT + "/sys/bus/pci/devices/0000:00:01.1/0000:01:00.0", exist_ok=True)

    acts = dg.Actions(enforce=True)
    g = dg.AerGuard(cfg("aer", mask_after=10), acts)
    t, cor = 0.0, 0
    out = captured(g.sample, t)
    for _ in range(3):  # below threshold
        cor += 2
        set_aer("0000:04:00.0", cor)
        t += 1
        out += captured(g.sample, t)
    check(out == "", "2 errors/s: below threshold, nothing")
    for _ in range(6):
        cor += 200
        set_aer("0000:04:00.0", cor)
        t += 1
        out += captured(g.sample, t)
    check(get("/sys/bus/pci/devices/0000:04:00.0/link/l1_aspm") == "0"
          and get("/sys/bus/pci/devices/0000:04:00.0/link/l1_2_aspm") == "0",
          "flood: ASPM L1/L1.x turned off on the link")
    check(get("/sys/bus/pci/devices/0000:04:00.0/link/clkpm") == "1", "clock PM left alone")
    check(cfg_dword("0000:04:00.0", 0x114) == 0x2000, "not masked yet (step 1 only)")
    for _ in range(12):
        cor += 200
        set_aer("0000:04:00.0", cor)
        t += 1
        out += captured(g.sample, t)
    check(cfg_dword("0000:04:00.0", 0x114) == 0x2000 | dg.AER_COR_MASK_ALL,
          "still flooding after mask_after: correctable errors masked in config space")
    check(out.count("PCIe error flood") == 1, "flood reported once")

    # root port: ASPM is turned off on the child that controls the link
    cor2 = 0
    out = ""
    for _ in range(6):
        cor2 += 100
        set_aer("0000:00:01.1", cor2)
        t += 1
        out += captured(g.sample, t)
    check(get("/sys/bus/pci/devices/0000:01:00.0/link/l1_aspm") == "0",
          "error flood on a root port: ASPM off via the child below it")

    # nonfatal errors are reported
    set_aer("0000:01:00.0", 0, nonfatal=3)
    t += 1
    out = captured(g.sample, t)
    check("nonfatal errors" in out, "nonfatal errors are logged")

    out = captured(acts.revert)
    check(get("/sys/bus/pci/devices/0000:04:00.0/link/l1_aspm") == "1"
          and get("/sys/bus/pci/devices/0000:01:00.0/link/l1_aspm") == "1"
          and cfg_dword("0000:04:00.0", 0x114) == 0x2000,
          "revert restores ASPM and the AER mask")


def test_usb():
    print("USB reconnect loops")
    reset_state()
    port = "/sys/devices/pci0000:00/0000:00:08.1/0000:0f:00.3/usb3/3-0:1.0/usb3-port2"
    put(port + "/disable", "0\n")
    devpath = "/devices/pci0000:00/0000:00:08.1/0000:0f:00.3/usb3/3-2"
    os.makedirs(ROOT + "/sys" + devpath, exist_ok=True)
    os.symlink(ROOT + port, ROOT + "/sys" + devpath + "/port")
    acts = dg.Actions(enforce=True)
    g = dg.UsbGuard(cfg("usb", reenable_after=120), acts)
    add = {"ACTION": "add", "SUBSYSTEM": "usb", "DEVTYPE": "usb_device",
           "DEVPATH": devpath, "PRODUCT": "46d/c52b/1211"}
    rem = dict(add, ACTION="remove")
    iface = dict(add, DEVTYPE="usb_interface", DEVPATH=devpath + "/3-2:1.0")
    out, t = "", 0.0
    for _ in range(5):  # 5 replugs in a minute: fine
        out += captured(g.event, add, t)
        out += captured(g.event, iface, t)
        out += captured(g.event, rem, t + 2)
        t += 10
    check(out == "" and get(port + "/disable") == "0", "5 replugs/minute: nothing")
    t += 120
    for _ in range(8):
        out += captured(g.event, add, t)
        out += captured(g.event, rem, t + 1)
        t += 3
    check(get(port + "/disable") == "1", "8 disconnects within 60 s: port disabled")
    check("046d:c52b" in out and "usb3-port2" in out, "log names VID:PID and port")
    captured(g.tick, t + 60)
    check(get(port + "/disable") == "1", "still disabled before reenable_after")
    out = captured(g.tick, t + 121)
    check(get(port + "/disable") == "0" and "re-enabling" in out, "re-enabled after reenable_after")
    # ignore by vid:pid
    g = dg.UsbGuard(cfg("usb", ignore=["046d:c52b"]), dg.Actions(enforce=True))
    for _ in range(10):
        captured(g.event, add, t)
        captured(g.event, rem, t + 1)
        t += 2
    check(get(port + "/disable") == "0", "ignored VID:PID is never disabled")
    # uevent parsing
    raw = b"remove@/devices/x/usb1/1-1\0ACTION=remove\0DEVPATH=/devices/x/usb1/1-1\0SUBSYSTEM=usb\0DEVTYPE=usb_device\0"
    env = dg.parse_uevent(raw)
    check(env.get("DEVTYPE") == "usb_device" and env.get("ACTION") == "remove", "netlink uevent parsing")


def test_kmsg():
    print("kernel log floods")
    reset_state()
    put("/proc/sys/kernel/printk", "7\t4\t1\t7\n")
    fifo = ROOT + "/dev/kmsg"
    os.makedirs(ROOT + "/dev", exist_ok=True)
    if os.path.exists(fifo):
        os.remove(fifo)
    os.mkfifo(fifo)
    acts = dg.Actions(enforce=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        g = dg.KmsgGuard(cfg("kmsg"), acts)
    w = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    for sec in range(6):
        for i in range(200):
            os.write(w, f"3,{sec * 200 + i},0,-;pcieport 0000:00:01.1: AER: Corrected error message received from 0000:01:00.{i % 8}\n SUBSYSTEM=pci\n".encode())
        g.readable()
        out.write(captured(g.tick, 1.0))
    os.close(w)
    text = out.getvalue()
    check(get("/proc/sys/kernel/printk").split()[0] == "3", "console loglevel lowered to 3")
    check("200 kernel log lines/s" in text and "AER: Corrected error message received from #:#:#.#" in text,
          "flood reported with rate and the typical message")
    captured(acts.revert)
    check(get("/proc/sys/kernel/printk").split()[0] == "7", "revert restores the loglevel")


def test_run_loop():
    print("service loop")
    reset_state()
    m = Machine()
    m.write()
    put("/proc/irq/87/smp_affinity_list", "0-3\n")
    c = dg.deep_merge(dg.DEFAULTS, {"mode": "enforce", "interval": 0.2,
                                     "irq": {"sustain": 2}, "usb": {"enabled": False}})
    import threading
    stop = threading.Event()

    def storm():
        while not stop.is_set():
            m.tick(1, {87: {2: 12000}}, {2: 0.9})  # 1 s worth per 0.2 s -> 60k/s
            stop.wait(0.2)
    th = threading.Thread(target=storm)
    th.start()
    out = captured(dg.run, c, 2.0, True)
    stop.set()
    th.join()
    check("interrupt storm" in out and get("/proc/irq/87/smp_affinity_list") == "0",
          "run() samples, detects and acts in enforce mode")
    out = captured(dg.status)
    check("move IRQ 87" in out, "status lists the recorded change")


def test_config():
    print("config")
    p = ROOT + "/devguard.toml"
    with open(p, "w") as f:
        f.write('mode = "enforce"\n[irq]\nhousekeeping_cpus = "15"\n[usb]\nenabled = false\n')
    c = dg.load_config(p)
    check(c["mode"] == "enforce" and c["irq"]["housekeeping_cpus"] == "15"
          and c["irq"]["sustain"] == 3 and not c["usb"]["enabled"],
          "TOML overrides merge with defaults")
    shipped = dg.load_config(os.path.join(HERE, "..", "devguard.toml"))
    check(shipped["mode"] == "observe", "shipped devguard.toml parses, observe by default")
    with open(p, "w") as f:
        f.write('mode = "yolo"\n')
    try:
        dg.load_config(p)
        check(False, "invalid mode rejected")
    except SystemExit:
        check(True, "invalid mode rejected")


if __name__ == "__main__":
    try:
        test_irq()
        test_aer()
        test_usb()
        test_kmsg()
        test_run_loop()
        test_config()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{'all tests passed' if not FAILS else f'{FAILS} FAILED'}")
    sys.exit(1 if FAILS else 0)
