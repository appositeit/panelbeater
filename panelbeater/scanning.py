# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Running a scan batch over the network transport.

The scanner returns JPEG directly, so there is no image decoding here -- the
bytes are written out as they arrive.
"""

from __future__ import annotations

import time
from typing import Callable

from .protocol import hw_status
from .session import Session

# Captured from ScanSnap Home (300 dpi colour). scan_params() patches the known
# fields; the rest is replayed as is. See docs/PROTOCOL.md.
D4_PARAMS = bytes.fromhex(
    "00030101d000c1808080908080008001000000000000000000000000000000300010012c012c0581"
    "0000000028d0000045a4000000000000000000000000000000000000000000000000000000000000"
)
assert len(D4_PARAMS) == 80, "d4 parameter block must be exactly 80 bytes"

D4_XRES = 0x22  # u16 BE, dots per inch
D4_YRES = 0x24  # u16 BE
D4_COMPOSITION = 0x26  # as in SET WINDOW over USB

# Verified on hardware. 600 dpi dropped the connection on two of three sheets.
NETWORK_RESOLUTIONS = (150, 200, 300)
COMPOSITION = {"color": 0x05, "gray": 0x02}


def d4_params(overrides: dict[int, bytes] | None = None) -> bytes:
    """The captured d4 block with raw offset -> bytes replaced, for experiments."""
    buf = bytearray(D4_PARAMS)
    for off, val in (overrides or {}).items():
        if off < 0 or off + len(val) > len(buf):
            raise ValueError(f"d4 override at {off:#x} runs off the 80-byte block")
        buf[off : off + len(val)] = val
    return bytes(buf)


def scan_params(
    resolution: int = 300, mode: str = "color", log: Callable[[str], None] = print
) -> bytes:
    """The d4 block for a verified resolution and mode; anything else falls back."""
    if resolution not in NETWORK_RESOLUTIONS:
        log(f"{resolution} dpi is not available over the network; using 300")
        resolution = 300
    mode = mode.strip().lower()
    if mode == "lineart":
        # Composition 0 yields no image; this transport only carries JPEG.
        log("lineart is not available over the network; scanning gray")
        mode = "gray"
    if mode not in COMPOSITION:
        log(f"unknown mode {mode!r}; scanning color")
        mode = "color"
    dpi = resolution.to_bytes(2, "big")
    return d4_params(
        {D4_XRES: dpi, D4_YRES: dpi, D4_COMPOSITION: bytes([COMPOSITION[mode]])}
    )


def describe(params: bytes) -> str:
    """'300 dpi color', read back out of a d4 block."""
    dpi = int.from_bytes(params[D4_XRES : D4_XRES + 2], "big")
    names = {v: k for k, v in COMPOSITION.items()}
    comp = params[D4_COMPOSITION]
    return f"{dpi} dpi {names.get(comp, f'composition {comp:#04x}')}"


def as_jpeg(raw: bytes) -> bytes | None:
    """Image data arrives with a couple of leading bytes before the SOI."""
    i = raw.find(b"\xff\xd8\xff")
    if i < 0:
        return None
    j = raw.rfind(b"\xff\xd9")
    return raw[i : j + 2] if j > i else raw[i:]


def decode_sense(b: bytes) -> tuple[int, int, int, bool, bool] | None:
    """REQUEST SENSE, per sane-backends fujitsu-scsi.h.

    Byte 2 holds the sense key plus the EOM and ILI flags; ASC and ASCQ are at
    0x0c and 0x0d.
    """
    if len(b) < 0x0E:
        return None
    return (b[2] & 0x0F, b[0x0C], b[0x0D], bool(b[2] & 0x40), bool(b[2] & 0x20))


# (key, asc, ascq) -> (why the batch stopped, was it a clean finish).
BATCH_END = {
    (0x03, 0x80, 0x03): ("hopper empty", True),
    (0x03, 0x80, 0x01): ("paper jam", False),
    (0x03, 0x80, 0x02): ("cover open", False),
    (0x03, 0x80, 0x04): ("unusual paper", False),
    (0x03, 0x80, 0x07): ("double feed", False),
    (0x03, 0x80, 0x08): ("no paper picked", False),
}


def hopper_has_paper(host: str, mac: bytes) -> bool:
    """Is another sheet waiting?

    Asked on a SEPARATE connection. The scan connection has to carry the
    d5/d8/e9/d4 sequence and nothing in front of it, or d4 fails with -1.
    """
    try:
        g = hw_status(host, mac)
    except OSError:
        return False
    return len(g) > 4 and not (g[3] & 0x80)


def scan_batch(
    s: Session,
    out_prefix: str,
    max_sheets: int = 100,
    log: Callable[[str], None] = print,
    params: bytes = D4_PARAMS,
    simplex: bool = False,
) -> int:
    """Scan every sheet in the hopper. Returns the number of sides written.

    The caller must already have registered, opened a session, selected a
    profile and called connect(). The scanner always sends both sides; with
    `simplex` the back is read and dropped.
    """
    setup = [
        ("d5 (01)", [0xD5, 0, 0, 0x01, 0x08, 0x08], 8, bytes(8)),
        ("d8 begin", [0xD8, 0, 0, 0, 0, 0], 0, b""),
        ("e9 config", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], 0, b""),
        ("d4 params", [0xD4, 0, 0, 0, 0x50, 0], 0, params),
        ("d5 (00)", [0xD5, 0, 0, 0x00, 0x08, 0x08], 8, bytes(8)),
    ]
    for label, cdb, rl, out in setup:
        st, _ = s.scsi(bytes(cdb), rl, out)
        log(f"  {label:<10} {st}")
        if st != 0:
            log(f"  aborting: {label} failed")
            return 0

    time.sleep(2)

    # Sides are numbered sequentially rather than named, so a plain
    # lexicographic glob puts them in page order. "-back" sorts before
    # "-front", which silently reversed every sheet.
    pages = 0
    stopped = f"reached the {max_sheets}-sheet limit"
    done = False
    try:
        for sheet in range(1, max_sheets + 1):
            # e0 starts THIS sheet, not the batch. The USB capture reissues the
            # equivalent (SET_WINDOW + OBJ_POS + SCAN) for every page. Sending
            # one e0 for the whole batch makes sheet 2 read back 2 bytes with
            # paper still in the hopper.
            st, _ = s.scsi(bytes([0xE0, 0, 0, 0, 0, 0]))
            log(f"  sheet {sheet} e0 START {st}")
            if st != 0:
                stopped = f"e0 refused for sheet {sheet} (status {st})"
                break

            got = 0
            for side, tag, last in ((0x00, "front", 0), (0x80, "back", 1)):
                _st, raw = s.scsi(
                    bytes([0x28, 0, 0, 0x02, 0, side, 0x30, 0, 0, 0, last, 0]),
                    0x300000,
                    collect=True,
                    quiet=20,
                )
                jpg = as_jpeg(raw)
                if jpg and not jpg.endswith(b"\xff\xd9"):
                    # A side over the 0x300000-byte read would stop there.
                    log(f"  sheet {sheet} {tag:<6} image has no end marker; "
                        f"it is probably cut off")  # fmt: skip
                if jpg and len(jpg) > 10000 and side and simplex:
                    log(f"  sheet {sheet} back   dropped (simplex)")
                    got += 1
                elif jpg and len(jpg) > 10000:
                    pages += 1
                    path = f"{out_prefix}-{pages:04d}.jpg"
                    with open(path, "wb") as fh:
                        fh.write(jpg)
                    log(f"  sheet {sheet} {tag:<6} {len(jpg):>9,} bytes -> {path}")
                    got += 1
                else:
                    log(f"  sheet {sheet} {tag:<6} no image ({len(raw)} bytes)")

                _, sense = s.scsi(bytes([0x03, 0, 0, 0, 0x12, 0]), 0x12)
                d = decode_sense(sense)
                if d:
                    key, asc, ascq, eom, ili = d
                    # The sense expected to end a batch (key 0x3 / asc 0x80 /
                    # ascq 0x03, "hopper empty") never actually arrives on this
                    # transport -- the key stays 0 through the last sheet. The
                    # hopper check below is what ends the batch. This stays to
                    # catch real faults: jam, cover open, double feed.
                    log(
                        f"  sheet {sheet} {tag:<6} sense key={key:#x} "
                        f"asc={asc:#04x} ascq={ascq:#04x} "
                        f"eom={int(eom)} ili={int(ili)}"
                    )
                    end = BATCH_END.get((key, asc, ascq))
                    if end:
                        stopped, done = end[0], True
                    elif key != 0:
                        stopped = f"sense key {key:#x} asc {asc:#04x} ascq {ascq:#04x}"
                        done = True
                    if done:
                        log(f"  sheet {sheet} {tag:<6} -> {stopped}")
                        break
                s.scsi(bytes([0x28, 0, 0x80, 0, 0, side, 0, 0, 0x20, 0, 0, 0]), 0x20)

            if done:
                break
            if not got:
                stopped = "no image data"
                break
            # Decided BEFORE reading the next sheet, never after: a speculative
            # empty read is what wedges the panel.
            if not hopper_has_paper(s.host, s.mac):
                stopped = "hopper empty"
                break
        log(f"  batch ended: {stopped}")
    finally:
        # Terminate even if the batch failed. Without these the panel sits on
        # "Scanning..." and eventually reports the connection was lost -- and a
        # crashed handler is exactly when that happens.
        #
        # The e0 here starts a page with no paper behind it, which is how the
        # scanner is told the batch is over; d6 then closes it.
        for label, cdb in (
            ("e0 end", [0xE0, 0, 0, 0, 0, 0]),
            ("d6 finish", [0xD6, 0, 0, 0, 0, 0]),
        ):
            try:
                st, _ = s.scsi(bytes(cdb))
                log(f"  {label:<10} {st}")
            except OSError as exc:
                log(f"  {label:<10} could not send: {exc}")
    return pages


def scan_to_dir(
    host: str,
    host_id: str,
    out_prefix: str,
    prof_id: str = "",
    max_sheets: int = 100,
    skip_register: bool = False,
    log: Callable[[str], None] = print,
    params: bytes = D4_PARAMS,
    simplex: bool = False,
) -> int:
    """Full scan: register, select a profile, check for paper, scan."""
    s = Session(host, host_id)
    if skip_register:
        log("using the existing registration")
    else:
        ok, why = s.register(on_wait=lambda: log("  scanner held by another host..."))
        if not ok:
            log(f"  registration failed: {why}")
            return 0
        log("registered")

    log(f"  session    {s.open_session()}")

    prof = prof_id
    if not prof:
        ps = s.profiles()
        if not ps:
            log("  could not read the profile list")
            return 0
        prof = ps[0]["prof_id"]
        log(f"  profile    {ps[0].get('prof_name')!r} ({prof})")
    log(f"  select     {s.select_profile(prof)}")

    if not hopper_has_paper(host, s.mac):
        log("no paper in the ADF")
        return 0

    s.connect()
    try:
        return scan_batch(
            s, out_prefix, max_sheets, log=log, params=params, simplex=simplex
        )
    finally:
        s.close()
