# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Low-level USB probe for the ScanSnap iX1500 (04c5:159f).

Speaks the Fujitsu vendor USB transport directly (SCSI CDBs wrapped in a
31-byte envelope) so we can watch raw GET_HW_STATUS bytes change as the
touch panel is used.

Usage:
    ix1500_probe.py inquiry          # one-shot INQUIRY, dump vendor pages
    ix1500_probe.py poll             # continuous GET_HW_STATUS, print on change
    ix1500_probe.py poll --all       # print every poll, not just changes
    ix1500_probe.py raw C2 00 ...    # send an arbitrary CDB, dump the reply
"""

from __future__ import annotations

import errno
import glob
import os
import subprocess
import time

import usb.core
import usb.util

from .. import USB_PRODUCTS, USB_VENDOR as VID
EP_OUT, EP_IN = 0x02, 0x81

# Fujitsu USB transport constants (see SANE fujitsu backend, fujitsu.h)
USB_COMMAND_CODE = 0x43
USB_COMMAND_LEN = 0x1F  # 31
USB_COMMAND_OFFSET = 0x13  # 19
USB_STATUS_CODE = 0x53
USB_STATUS_LEN = 0x0D  # 13
USB_STATUS_OFFSET = 0x09  # 9

# SCSI opcodes used by the Fujitsu/ScanSnap family
TEST_UNIT_READY = 0x00
INQUIRY = 0x12
GET_HW_STATUS = 0xC2
SCANNER_CONTROL = 0xF1


class ScannerAbsent(RuntimeError):
    """The scanner is not on the USB bus at all (cover closed, or unplugged)."""


class ScannerBusy(RuntimeError):
    """The scanner is present but another process holds the interface."""


def _busy_message() -> str:
    """Explain who has the scanner.

    A bare "[Errno 16] Resource busy" traceback gives no hint of who has it.
    The usual answer is a virtual machine with the scanner passed through --
    `-device usb-host` hands it to QEMU and the host cannot open it until the
    VM stops -- so name the holder rather than the errno.
    """
    lines = ["The scanner is on the USB bus but already claimed by another process."]

    node = None
    for f in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if open(f).read().strip() != f"{VID:04x}":
                continue
            d = os.path.dirname(f)
            if int(open(os.path.join(d, "idProduct")).read(), 16) not in USB_PRODUCTS:
                continue
            bus = int(open(os.path.join(d, "busnum")).read())
            dev = int(open(os.path.join(d, "devnum")).read())
            node = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
        except (OSError, ValueError):
            continue

    holder = ""
    if node:
        try:
            out = subprocess.run(
                ["fuser", "-v", node], capture_output=True, text=True, timeout=5
            )
            holder = (out.stderr or out.stdout).strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    if holder:
        lines += ["", "  Held by:", "    " + holder.replace("\n", "\n    ")]
        if "qemu" in holder.lower() or "kvm" in holder.lower():
            lines += [
                "",
                "  That is a virtual machine with the scanner passed through.",
                "  Shut the VM down and try again.",
            ]
    else:
        lines += [
            "",
            "  Common causes: a virtual machine with USB passthrough, another",
            "  copy of panelbeater, or a scanning program holding the device.",
        ]
    return "\n".join(lines)


class Ix1500:
    def __init__(self, timeout_ms: int = 3000):
        self.timeout = timeout_ms
        self.dev = usb.core.find(
            idVendor=VID, custom_match=lambda d: d.idProduct in USB_PRODUCTS
        )
        if self.dev is None:
            ids = ", ".join(f"{VID:04x}:{p:04x}" for p in USB_PRODUCTS)
            raise ScannerAbsent(
                f"No ScanSnap ({ids}) is on the USB bus.\n"
                "  Is the ADF paper chute cover open? Closing it powers the\n"
                "  scanner off and removes it from USB entirely."
            )
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError as exc:
            if exc.errno == errno.EBUSY:
                # Message built lazily by the caller -- it shells out to fuser,
                # which must not run on every retry of a polling loop.
                raise ScannerBusy("scanner is claimed by another process") from None
            raise

    def release(self) -> None:
        """Let go of the USB device so another program can open it.

        SANE opens the scanner through libusb too, and only one process can
        claim the interface: while this daemon holds it, scanimage fails at
        open with "Invalid argument". Anything shelling out to SANE must
        release first and re-acquire afterwards.
        """
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001 -- releasing must never raise
            pass

    def command(self, cdb: bytes, read_len: int = 0, payload: bytes | None = None):
        """Send one CDB. Returns (data, status_byte)."""
        env = bytearray(USB_COMMAND_LEN)
        env[0] = USB_COMMAND_CODE
        env[USB_COMMAND_OFFSET : USB_COMMAND_OFFSET + len(cdb)] = cdb
        self.dev.write(EP_OUT, env, self.timeout)

        if payload:
            self.dev.write(EP_OUT, payload, self.timeout)

        data = b""
        if read_len:
            data = bytes(self.dev.read(EP_IN, read_len, self.timeout))

        status = bytes(self.dev.read(EP_IN, USB_STATUS_LEN, self.timeout))
        return data, status

    def test_unit_ready(self):
        return self.command(bytes([TEST_UNIT_READY] + [0] * 5))

    def inquiry(self, page: int = 0, length: int = 0x60):
        cdb = bytes([INQUIRY, 0x00, page, 0x00, length, 0x00])
        return self.command(cdb, length)

    def hw_status(self, length: int = 0x20):
        cdb = bytes([GET_HW_STATUS, 0, 0, 0, 0, 0, 0, length >> 8, length & 0xFF, 0])
        return self.command(cdb, length)

    def scanner_control(self, function: int, extra: bytes = b""):
        """SCANNER_CONTROL (0xf1). function goes in byte 1 low nibble."""
        cdb = bytearray(10)
        cdb[0] = SCANNER_CONTROL
        cdb[1] = function & 0x0F
        cdb[2] = (function >> 4) & 0xFF
        for i, b in enumerate(extra):
            cdb[5 + i] = b
        return self.command(bytes(cdb))


# Bit layout transcribed verbatim from the SANE fujitsu backend
# (backend/fujitsu-scsi.h, the get_GHS_* macros).  offset -> [(mask, name), ...]
GHS_BITS = {
    0x02: [
        (0x80, "top"),
        (0x20, "fedalm"),
        (0x10, "adjalm"),
        (0x08, "A3"),
        (0x04, "B4"),
        (0x02, "A4"),
        (0x01, "B5"),
    ],
    0x03: [
        (0x80, "hopper_empty"),
        (0x40, "omr"),
        (0x20, "adf_open"),
        (0x10, "imp_open"),
        (0x08, "fb_open"),
        (0x04, "paper_end"),
        (0x02, "fb_on"),
        (0x01, "exit"),
    ],
    0x04: [
        (0x80, "sleep"),
        (0x40, "clean"),
        (0x20, "scan_sw_long"),
        (0x10, "hpos"),
        (0x04, "send_sw"),
        (0x02, "manual_feed"),
        (0x01, "SCAN_SW"),
    ],
    0x05: [(0x80, "picalm"), (0x40, "padalm"), (0x20, "brkalm"), (0x10, "sepalm")],
    0x06: [
        (0x80, "ink_empty"),
        (0x40, "consume"),
        (0x20, "overskew"),
        (0x10, "overthick"),
        (0x08, "plen"),
        (0x04, "ink_side"),
        (0x02, "mf_to"),
        (0x01, "double_feed"),
    ],
    0x0E: [
        (0x80, "adjalm_fed"),
        (0x10, "non_sep"),
        (0x04, "ext_sendto"),
        (0x02, "rq_hldimg"),
        (0x01, "pacnt"),
    ],
    0x10: [
        (0x80, "wifi_sw"),
        (0x40, "w_use"),
        (0x20, "w_use2"),
        (0x10, "w_use3"),
        (0x08, "w_use4"),
    ],
    0x11: [
        (0x80, "battery"),
        (0x40, "btr_charge"),
        (0x20, "btr_chg_tmp_stp"),
        (0x10, "ibtr_ene_sav"),
        (0x04, "fngr_caut"),
        (0x02, "trnpg_l"),
        (0x01, "trnpg_r"),
    ],
}

# offset -> (name, width) for whole-byte / multi-byte fields
GHS_FIELDS = [
    (0x05, "function", 1, 0x0F),
    (0x07, "error_code", 1, 0xFF),
    (0x09, "skew_angle", 1, 0xFF),
    (0x0A, "ink_remain", 1, 0xFF),
    (0x0C, "lang_code", 2, 0xFFFF),
    (0x12, "btr_power", 1, 0xFF),
]


def decode(data: bytes) -> str:
    out = []
    for idx, entries in sorted(GHS_BITS.items()):
        if idx >= len(data):
            continue
        for mask, name in entries:
            if data[idx] & mask:
                out.append(name)
    for off, name, width, mask in GHS_FIELDS:
        if off + width > len(data):
            continue
        val = int.from_bytes(data[off : off + width], "big") & mask
        if val:
            out.append(f"{name}={val}")
    return " ".join(out) or "-"


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def cmd_inquiry(dev: Ix1500, _args):
    data, status = dev.inquiry()
    print(f"INQUIRY std  status={hexs(status)}")
    print(f"  {hexs(data)}")
    print(
        f"  vendor={data[8:16].decode('ascii', 'replace')!r} "
        f"product={data[16:32].decode('ascii', 'replace')!r} "
        f"rev={data[32:36].decode('ascii', 'replace')!r}"
    )
    print(f"  ascii: {data.decode('ascii', 'replace')}")
    for page in (0xF0, 0xF1, 0xF2):
        try:
            data, status = dev.inquiry(page=page, length=0x40)
            print(f"INQUIRY page {page:#04x} status={hexs(status)}")
            print(f"  {hexs(data)}")
        except usb.core.USBError as exc:
            print(f"INQUIRY page {page:#04x} failed: {exc}")


def cmd_poll(dev: Ix1500, args):
    print("Polling GET_HW_STATUS every %dms.  Ctrl-C to stop." % args.interval)
    print("Try: press Scan on the panel, open/close the ADF cover, insert paper.")
    print()
    last = None
    t0 = time.monotonic()
    while True:
        try:
            data, status = dev.hw_status(args.length)
        except usb.core.USBError as exc:
            print(f"[{time.monotonic() - t0:7.2f}] USB error: {exc}")
            time.sleep(0.5)
            continue
        if args.all or data != last:
            print(
                f"[{time.monotonic() - t0:7.2f}] {hexs(data)}  st={hexs(status[:2])}  {decode(data)}"
            )
            last = data
        time.sleep(args.interval / 1000.0)


def cmd_raw(dev: Ix1500, args):
    cdb = bytes(int(x, 16) for x in args.cdb)
    data, status = dev.command(cdb, args.read)
    print(f"CDB    {hexs(cdb)}")
    print(f"DATA   {hexs(data)}")
    print(f"STATUS {hexs(status)}")
