# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scan on the iX1500 over USB, without SANE.

Why not SANE: it opens the scanner through libusb too, and only one process can
claim the interface. Shelling out to scanimage means releasing the device for the
whole scan -- blind to the Stop button, blind to errors, and "finished" has to be
inferred from a subprocess exit code. Doing the scan here keeps the connection.

Why not the vendor commands the network path uses: they are network-only.
Sending d5/d8/e9/d4 over USB gets Overflow on the first one and timeouts after,
so USB has to use standard SCSI, the way ScanSnap Home does. The consequence is
raw pixel data rather than the JPEG the network transport returns -- about 25MB
a side at 300dpi colour instead of 650KB -- so it is compressed here before
anything else touches it.

Sequence, taken from captures/scansnap_home/usb-fullbus.pcapng:

    setup      TEST UNIT READY
               MODE SELECT pages 3c 3a 38 39 34 35
               SET WINDOW (64-byte descriptor)
               WRITE 2a 00 83   gamma/LUT table
               WRITE 2a 00 88   second table
    per sheet  OBJECT POSITION 31 01   feed
               SCAN 1b
               READ 28 ... window 0x00 front / 0x80 back, alternating
               READ 28 00 80 / 28 00 81   page info
               SCANNER CONTROL f1 09
    finish     MODE SELECT page 2c value 05

The two WRITE tables are replayed verbatim. They are a gamma ramp and something
similar; nothing depends on understanding them, so they are captured constants
until they need to change.
"""

from __future__ import annotations

import io
import time

import numpy as np
from PIL import Image

from .sequence import SETUP_SEQUENCE

# Field values for the SET WINDOW descriptor and the image decoder.
COMPOSITION = {"lineart": 0x00, "gray": 0x02, "color": 0x05}
BITS = {"lineart": 1, "gray": 8, "color": 8}
CHANNELS = {"lineart": 1, "gray": 1, "color": 3}

WINDOW_FRONT = 0x00
WINDOW_BACK = 0x80
CHUNK = 0xFFEE  # what ScanSnap Home asks for per READ
WINDOW_WIDTH_1200 = 10448  # scan width in 1/1200 inch, from SET WINDOW
FILLER = 0x55  # what the scanner streams once the sheet has passed

# The hand-typed MODE SELECT pages, gamma tables and SET WINDOW descriptor that
# used to live here have been removed on purpose. Every one of them was
# truncated -- the SET WINDOW payload had a single 64-byte window descriptor
# where the scanner sends two, one per side, which stalled duplex in a way that
# looked like a transport fault. setup() replays sequence.py, captured byte for
# byte, and patch() edits only the fields it understands. If you need to change
# something, patch the captured bytes; do not retype them.


class UsbScanner:
    """Standard-SCSI scanning over the raw USB transport."""

    def __init__(self, dev, resolution: int = 300, mode: str = "color",
                 duplex: bool = True, verbose: bool = False):  # fmt: skip
        self.dev = dev
        self.verbose = verbose
        self.resolution = resolution
        self.mode = mode
        self.duplex = duplex
        self.width_px = 0
        self.last_fault = None
        self.feed_settle = 0.8

    # -- helpers ----------------------------------------------------------
    def _cmd(self, cdb, read_len: int = 0, payload: bytes | None = None):
        """Send one command.

        With verbose on, the CDB is printed BEFORE it goes out: a USB timeout
        raises from deep inside libusb and the traceback never says which
        command caused it, so without this a hang is anonymous.
        """
        if self.verbose:
            print(f"    cmd {' '.join(f'{b:02x}' for b in cdb)}"
                  f"{f' +{len(payload)}B' if payload else ''}"
                  f"{f' read {read_len}' if read_len else ''}", flush=True)  # fmt: skip
        return self.dev.command(bytes(cdb), read_len, payload)

    def sense(self) -> tuple[int, int, int, bool, bool]:
        """(key, asc, ascq, eom, ili) -- offsets per fujitsu-scsi.h."""
        data, _ = self._cmd([0x03, 0, 0, 0, 0x12, 0], 0x12)
        if len(data) < 0x0E:
            return (0, 0, 0, False, False)
        return (
            data[2] & 0x0F, data[0x0C], data[0x0D],
            bool(data[2] & 0x40), bool(data[2] & 0x20),
        )  # fmt: skip

    # -- batch ------------------------------------------------------------
    def patch(self, cdb: bytes, payload: bytes | None) -> bytes | None:
        """Apply our settings to a captured payload.

        Only fields we understand are touched; everything else is replayed as
        captured. Resolution appears in THREE places -- both window descriptors
        in SET WINDOW and the SET PRE READMODE diagnostic -- and missing any of
        them leaves the scanner disagreeing with itself.
        """
        if payload is None:
            return None
        buf = bytearray(payload)
        if cdb[0] == 0x24:  # SET WINDOW: 8-byte header then TWO 64-byte descriptors
            for wd in (8, 8 + 64):
                if wd + 0x22 <= len(buf):
                    buf[wd + 0x02 : wd + 0x04] = self.resolution.to_bytes(2, "big")
                    buf[wd + 0x04 : wd + 0x06] = self.resolution.to_bytes(2, "big")
                    buf[wd + 0x19] = COMPOSITION[self.mode]
                    buf[wd + 0x1A] = BITS[self.mode]
                    # Uncompressed: ScanSnap Home asks for 0x81 (JPG1) but the
                    # stream is headerless and would need reconstructing.
                    buf[wd + 0x20] = 0x00
                    buf[wd + 0x21] = 0x00
        elif cdb[0] == 0x1D and buf[:16] == b"SET PRE READMODE":
            buf[0x10:0x12] = self.resolution.to_bytes(2, "big")
            buf[0x12:0x14] = self.resolution.to_bytes(2, "big")
            buf[0x1C] = COMPOSITION[self.mode]
        return bytes(buf)

    def setup(self) -> None:
        """Replay the captured setup, patched for our settings.

        Driven by the capture rather than reconstructed. Every constant that
        was hand-typed here turned out to be truncated.
        """
        for cdb_hex, pl_hex in SETUP_SEQUENCE:
            cdb = bytes.fromhex(cdb_hex)
            if cdb[0] == 0x31:  # the feed belongs to start_sheet, not setup
                continue
            payload = bytes.fromhex(pl_hex) if pl_hex else None
            rl = int.from_bytes(cdb[7:9], "big") if cdb[0] == 0xC2 else 0
            self._cmd(list(cdb), rl, self.patch(cdb, payload))
        # Width comes from the SET WINDOW descriptor: 10448/1200 inch. Deriving
        # it as int(8.71 * res) rounds to 2613 at 300dpi instead of 2612, and a
        # one-pixel stride error shears the page diagonally across 3500 rows.
        self.width_px = WINDOW_WIDTH_1200 * self.resolution // 1200

    def start_sheet(self) -> int | None:
        """Feed a sheet and start scanning it.

        Returns None on success, or the ASCQ of the fault that stopped it.
        Checks the hopper BEFORE feeding: afterwards "hopper empty" just means
        that was the last sheet, and treating it as failure breaks single-sheet
        loads. Nothing is sent between the feed and SCAN -- the gap in the
        capture is OBJECT POSITION blocking while the sheet feeds, and slipping
        a REQUEST SENSE in there earns a command-sequence error.
        """
        g, _ = self.dev.hw_status(0x30)
        if len(g) > 3 and (g[3] & 0x80):
            self.last_fault = 0x03
            return 0x03
        self._cmd([0x31, 0x01, 0, 0, 0, 0, 0, 0, 0, 0])  # OBJECT POSITION feed
        windows = (
            bytes([WINDOW_FRONT, WINDOW_BACK]) if self.duplex else bytes([WINDOW_FRONT])
        )
        self._cmd([0x1B, 0, 0, 0, len(windows), 0], 0, windows)
        return None

    def max_bytes(self) -> int:
        """Ceiling for one side, from the page geometry plus 20% slack.

        A missed end-of-page once produced a 1.1GB buffer -- the scanner keeps
        answering reads long after the sheet has passed. Never trust EOM alone.
        """
        rows = int(14.86 * self.resolution)
        return int(self.width_px * CHANNELS[self.mode] * rows * 1.2)

    def read_sheet(self, timeout_s: float = 120.0) -> dict[int, bytes]:
        """Read BOTH sides of a sheet, interleaved.

        ScanSnap Home alternates between the two windows, reading whichever is
        ready. Servicing only the front lets the back buffer fill; the scanner
        then stalls mid-sheet and the panel drops to "Error".

        End of a side is REQUEST SENSE reporting EOM with key 0 -- not a medium
        error. While scanning it answers key 0x3 / asc 0x80 / ascq 0x13, which
        means "not ready yet", so that is a wait, not a fault.
        """
        windows = [WINDOW_FRONT, WINDOW_BACK] if self.duplex else [WINDOW_FRONT]
        chunk = {WINDOW_FRONT: 0xFFEE, WINDOW_BACK: 0x010000}
        buf = {w: bytearray() for w in windows}
        done = {w: False for w in windows}
        cap = self.max_bytes()
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline and not all(done.values()):
            for w in windows:
                if done[w]:
                    continue
                n = chunk[w]
                self._cmd([0xF1, 0x10, w, 0, 0, 0,
                           (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF, 0])  # fmt: skip
                key, asc, ascq, eom, ili = self.sense()
                if key == 0x03 and asc == 0x80:
                    if ascq == 0x13:
                        continue  # still scanning; give the other window a turn
                    self.last_fault = ascq
                    done[w] = True
                    continue
                try:
                    data, _ = self._cmd(
                        [0x28, 0, 0, 0, 0, w,
                         (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF, 0], n
                    )  # fmt: skip
                except Exception:  # noqa: BLE001
                    done[w] = True
                    continue
                if data:
                    buf[w] += data
                    # The scanner keeps answering reads after the sheet has
                    # passed, streaming 0x55 filler. EOM alone proved unreliable
                    # -- it never fired and a side ran to 1.1GB -- so a chunk
                    # that is essentially all filler ends the page. Not exactly
                    # all: demanding 100% missed it on a real scan and read the
                    # full 42MB ceiling instead, so allow a few stray bytes.
                    if len(data) > 4096 and data.count(FILLER) >= len(data) * 0.95:
                        done[w] = True
                if eom or not data:
                    done[w] = True
                elif len(buf[w]) > cap:
                    # Belt and braces: the sheet is physically finished long
                    # before this, so something is wrong with EOM detection.
                    print(f"    window {w:#04x}: hit the {cap:,}B ceiling, stopping",
                          flush=True)  # fmt: skip
                    done[w] = True
        return {w: bytes(b) for w, b in buf.items()}

    def to_jpeg(self, raw: bytes, quality: int = 90) -> bytes | None:
        """Raw scanner pixels -> JPEG.

        Two corrections the scanner does not make for you:

        * The data is INVERTED -- white paper arrives near 0, so a straight
          render is a photographic negative.
        * The tail is 0x55 filler. The scanner keeps answering reads long after
          the sheet has passed, so the trailing constant rows are trimmed.
        """
        ch = CHANNELS[self.mode]
        stride = self.width_px * ch
        if stride == 0 or len(raw) < stride * 10:
            return None
        rows = len(raw) // stride
        arr = np.frombuffer(raw[: rows * stride], dtype=np.uint8).reshape(
            rows, self.width_px, ch
        )
        flat = arr.reshape(rows, -1)
        is_filler = (flat.std(axis=1) < 0.5) & (
            np.abs(flat.mean(axis=1) - FILLER) < 1.0
        )
        last = rows
        for i in range(rows - 1, -1, -1):
            if not is_filler[i]:
                last = i + 1
                break
        if last < 10:
            return None
        img = 255 - arr[:last]
        out = io.BytesIO()
        Image.fromarray(img.squeeze() if ch == 1 else img).save(
            out, "JPEG", quality=quality, dpi=(self.resolution, self.resolution)
        )
        return out.getvalue()

    def finish(self) -> None:
        self._cmd([0x31, 0x02, 0, 0, 0, 0, 0, 0, 0, 0])
        out = bytes.fromhex("000000002c06050000000000")
        self._cmd([0x15, 0x10, 0, 0, len(out), 0], 0, out)

    def scan_batch(self, out_prefix: str, max_sheets: int = 100) -> int:
        """Scan until the feeder empties. Returns the number of sides written."""
        self.setup()
        pages = 0
        try:
            for sheet in range(1, max_sheets + 1):
                fault = self.start_sheet()
                if fault is not None:
                    reason = {0x03: "hopper empty", 0x01: "paper jam",
                              0x02: "cover open", 0x07: "double feed"}.get(fault, f"ascq {fault:#x}")  # fmt: skip
                    print(f"  sheet {sheet}: {reason}")
                    break
                sides = self.read_sheet()
                got = 0
                for w, tag in ((WINDOW_FRONT, "front"), (WINDOW_BACK, "back")):
                    raw = sides.get(w, b"")
                    jpeg = self.to_jpeg(raw)
                    if jpeg and len(jpeg) > 8000:
                        pages += 1
                        path = f"{out_prefix}-{pages:04d}.jpg"
                        with open(path, "wb") as fh:
                            fh.write(jpeg)
                        print(f"  sheet {sheet} {tag:<5} {len(raw):>10,}B raw -> "
                              f"{len(jpeg):>8,}B  {path}")  # fmt: skip
                        got += 1
                    elif raw:
                        print(
                            f"  sheet {sheet} {tag:<5} {len(raw)}B raw, no usable image"
                        )
                self._cmd([0xF1, 0x09, 0, 0, 0, 0, 0, 0, 0, 0])  # scan complete
                if not got:
                    break
        finally:
            self.finish()
        return pages
