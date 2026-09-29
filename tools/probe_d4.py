#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Try variants of the network d4 parameter block against a real scanner.

Stop the daemon first -- this registers on its own and two registrants fight:

    systemctl --user stop panelbeater      (or: sudo systemctl stop panelbeater@you)

    tools/probe_d4.py                                   captured block, setup only
    tools/probe_d4.py --set 0x22=0096 --set 0x24=0096   150 dpi, setup only
    tools/probe_d4.py --set 0x26=02 --scan              gray, scan the hopper

Setup-only runs d5/d8/e9/d4/d5 and then the batch terminators, so it needs no
paper and reports whether the scanner accepted the block. --scan scans what is
in the hopper -- load one sheet -- and reports what the JPEGs actually are:
size, pixel dimensions, density and number of components. That is the only
trustworthy check -- a d4 that is accepted can still be ignored.
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from panelbeater.config import Config  # noqa: E402
from panelbeater.scanning import d4_params, scan_to_dir  # noqa: E402
from panelbeater.session import Session  # noqa: E402


def jpeg_info(data: bytes) -> dict:
    """Width, height, components and JFIF density, stdlib only."""
    info: dict = {}
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
        body = data[i + 4 : i + 2 + seg]
        if marker == 0xE0 and body[:5] == b"JFIF\x00" and len(body) >= 12:
            info["density"] = (body[7], *struct.unpack(">HH", body[8:12]))
        if marker in (0xC0, 0xC1, 0xC2) and len(body) >= 6:
            h, w = struct.unpack(">HH", body[1:5])
            info.update(height=h, width=w, components=body[5], bits=body[0])
            break
        if marker == 0xDA:
            break
        i += 2 + seg
    return info


def parse_set(items: list[str]) -> dict[int, bytes]:
    out = {}
    for it in items:
        off, _, val = it.partition("=")
        out[int(off, 0)] = bytes.fromhex(val)
    return out


def setup_only(host: str, host_id: str, params: bytes, prof: str) -> int:
    s = Session(host, host_id)
    ok, why = s.register()
    if not ok:
        print(f"registration failed: {why}")
        return 2
    print(f"session {s.open_session()}")
    prof = prof or s.profiles()[0]["prof_id"]
    print(f"select  {s.select_profile(prof)}")
    s.connect()
    worst = 0
    try:
        for label, cdb, rl, out in (
            ("d5 (01)", [0xD5, 0, 0, 0x01, 0x08, 0x08], 8, bytes(8)),
            ("d8", [0xD8, 0, 0, 0, 0, 0], 0, b""),
            ("e9", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], 0, b""),
            ("d4", [0xD4, 0, 0, 0, 0x50, 0], 0, params),
            ("d5 (00)", [0xD5, 0, 0, 0x00, 0x08, 0x08], 8, bytes(8)),
        ):
            st, _ = s.scsi(bytes(cdb), rl, out)
            print(f"  {label:<8} {st}")
            if st != 0:
                worst = st
                break
    finally:
        for label, cdb in (("e0 end", [0xE0, 0, 0, 0, 0, 0]), ("d6", [0xD6, 0, 0, 0, 0, 0])):
            try:
                print(f"  {label:<8} {s.scsi(bytes(cdb))[0]}")
            except OSError as exc:
                print(f"  {label:<8} {exc}")
        s.close()
        print(f"unregister {s.unregister()}")
    return 0 if worst == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scanner", default="")
    ap.add_argument("--set", action="append", default=[], metavar="OFF=HEX")
    ap.add_argument("--scan", action="store_true", help="scan the hopper and inspect the JPEGs")
    # A limit reached with paper loaded feeds the next sheet through unread.
    ap.add_argument("--sheets", type=int, default=100, help="stop after this many sheets")
    ap.add_argument("--keep", default="", help="directory to keep the JPEGs in")
    ap.add_argument("--prof-id", default="")
    a = ap.parse_args()

    cfg = Config.load()
    host = a.scanner or cfg.get("scanner")
    if not host or host == "auto":
        print("set `scanner = <ip>` in the config or pass --scanner")
        return 2
    params = d4_params(parse_set(a.set))
    for i in range(0, 80, 16):
        print(f"d4 {i:02x}: " + " ".join(f"{b:02x}" for b in params[i : i + 16]))

    if not a.scan:
        return setup_only(host, cfg.host_id, params, a.prof_id or cfg.get("prof_id"))

    work = Path(a.keep or tempfile.mkdtemp(prefix="probe-d4-"))
    work.mkdir(parents=True, exist_ok=True)
    n = scan_to_dir(host, cfg.host_id, str(work / "page"), prof_id=a.prof_id or cfg.get("prof_id"),
                    max_sheets=a.sheets, params=params)
    s = Session(host, cfg.host_id)
    print(f"unregister {s.unregister()}")
    for p in sorted(work.glob("page-*.jpg")):
        print(f"{p.name}: {p.stat().st_size:,} bytes {jpeg_info(p.read_bytes())}")
    print(f"{n} side(s) in {work}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
