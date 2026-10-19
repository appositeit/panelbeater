# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Watch the Scan button over USB and scan when it is pressed.

Three things have to happen, and all three were hard-won:

  1. ARM THE PANEL. Without it the Scan button is greyed out and a press is not
     even reported. Arming is a `SETUP PROF INFO` write, subject 0x02,
     direction 0x10, carrying `current_user_id` -- 32 hex characters, NOT the
     16-character host_id used for network registration. Sending the 16-char
     one is accepted and does nothing.

  2. ANSWER PROMPTLY. A press opens a job and the scanner waits for the host.
     Answer within seconds and it scans; leave it and the job goes stale and
     the panel drops to "Error" until the ADF cover is cycled. Hence the 50ms
     poll: a press can be visible for as little as one poll.

  3. FINISH THE BATCH. When the feeder empties the panel sits on "Scanning..."
     forever. `OBJECT POSITION 31 02` moves it to the ADF-empty prompt, and
     `MODE SELECT` page 0x2c with byte 6 = 0x05 returns it to the profile
     screen. The obvious candidates -- f1 09, f1 04, e0, d6, 31 00, 31 04 --
     are all accepted and all do nothing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .. import output
from ..config import Config
from .panel import Panel
from .scanner import UsbScanner, is_ix1600
from .transport import Ix1500, ScannerAbsent, ScannerBusy

SCAN_SW_MASK = 0x01
HOPPER_EMPTY_MASK = 0x80


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def user_id_from_scanner(panel: Panel, log=print) -> str:
    """The 32-hex id the panel acts as, read from the scanner's own profiles.

    Per-scanner, so it cannot be a constant. An earlier version hard-coded one
    particular unit's value, which worked only on that unit.
    """
    try:
        raw = panel.transact(0x01)
    except Exception as exc:  # noqa: BLE001
        log(f"  could not read profiles: {exc}")
        return ""
    i = raw.find(b'{"')
    if i < 0:
        return ""
    try:
        doc = json.loads(raw[i:].split(b"\x00")[0].decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return ""
    for p in doc.get("profiles", []):
        if p.get("user_id"):
            return str(p["user_id"])
    return ""


MODE_PAGE_SLEEP = 0x34


def read_sleep_timer(dev: Ix1500) -> int | None:
    """Minutes of idleness before the panel sleeps, or None if unreadable.

    The page starts at offset 12 of the MODE SENSE reply -- there is an 8-byte
    block descriptor first. Reading from offset 4 lands in that descriptor,
    which is always zero, and looks exactly like a disabled timer.
    """
    try:
        d, _ = dev.command(bytes([0x1A, 0x00, MODE_PAGE_SLEEP, 0x00, 0x14, 0x00]), 0x14)
    except Exception:  # noqa: BLE001
        return None
    return d[14] if len(d) > 14 else None


def set_sleep_timer(dev: Ix1500, minutes: int, log=print) -> int | None:
    """Set the timer and return what the scanner actually stored.

    Values are clamped silently and the write always reports success: 1 becomes
    2 (the minimum), 255 becomes 224 (the maximum). So the write is verified by
    reading it back rather than trusting the status.
    """
    out = bytes(4) + bytes([MODE_PAGE_SLEEP, 0x06, minutes & 0xFF, 0, 0, 0, 0, 0])
    try:
        dev.command(bytes([0x15, 0x10, 0, 0, len(out), 0]), 0, out)
    except Exception as exc:  # noqa: BLE001
        log(f"  could not set the sleep timer: {exc}")
        return None
    got = read_sleep_timer(dev)
    if got is not None and got != minutes:
        log(f"  sleep timer: asked for {minutes} min, scanner stored {got}")
    return got


KEEPALIVE_S = 10.0


def open_session(dev: Ix1500, user_id: str) -> None:
    """Write the host session document (panel subject 0x02).

    ScanSnap Home rewrites it every 10s, the first time 12.6s after a batch
    ends (iX1600 capture). Without it, an iX1600 panel reported the
    connection lost about 10s after a batch and recovered ~30s later.
    """
    doc = {
        "version": 1,
        "function_level": 2,
        "current_user_id": user_id,
        "host_address": "",  # empty over USB; an IP over Wi-Fi
        "host_port": 53220,
    }
    Panel(dev).transact(0x02, json.dumps(doc, separators=(",", ":")).encode(), write=True)


def arm(dev: Ix1500, user_id: str, log=print) -> bool:
    """Put the panel into the state where the Scan button works."""
    try:
        dev.command(bytes([0x00, 0, 0, 0, 0, 0]))  # TEST UNIT READY
        now = time.localtime()
        payload = b"FIRST READ DATE " + bytes(
            [0, now.tm_year % 100, now.tm_mon, now.tm_mday,
             now.tm_hour, now.tm_min, now.tm_sec, 0xFF, 0xFF, 0x73, 0x60]
        )  # fmt: skip
        dev.command(
            bytes([0x1D, 0, 0, len(payload) >> 8, len(payload) & 0xFF, 0]), 0, payload
        )
        open_session(dev, user_id)
        # Page 0x2c value 0x06 is the "ready" setting ScanSnap Home writes when
        # it attaches; 0x05 is what it writes when a batch finishes.
        out = bytes.fromhex("000000002c06060000000000")
        dev.command(bytes([0x15, 0x10, 0, 0, len(out), 0]), 0, out)
        return True
    except Exception as exc:  # noqa: BLE001 -- never let arming kill the daemon
        log(f"[{stamp()}] arming failed: {exc}")
        return False


def finish_batch(dev: Ix1500, log=print) -> None:
    """Return the panel from "Scanning..." to the profile screen."""
    try:
        dev.command(bytes([0x31, 0x02, 0, 0, 0, 0, 0, 0, 0, 0]))
        out = bytes.fromhex("000000002c06050000000000")
        dev.command(bytes([0x15, 0x10, 0, 0, len(out), 0]), 0, out)
    except Exception as exc:  # noqa: BLE001
        log(f"[{stamp()}] could not finish the batch: {exc}")


def serve(cfg: Config, log=print) -> int:
    try:
        dev = Ix1500()
    except ScannerBusy as exc:
        log(str(exc))
        return 1
    except ScannerAbsent as exc:
        log(str(exc))
        return 1

    user_id = cfg.get("user_id") or user_id_from_scanner(Panel(dev), log=log)
    if not user_id:
        log(
            "could not work out this scanner's user id, and the panel cannot be\n"
            "armed without it. Set `user_id` in the config if you know it."
        )
        return 1

    log(f"[{stamp()}] arming the panel as {user_id}")
    if not arm(dev, user_id, log=log):
        return 1

    # dim_after is a network idea -- it stops registering, and there is no
    # registration here. Over USB the panel dims purely on the scanner's own
    # timer, which unlike the network transport CAN be set from this side.
    want = int(cfg.num("dim_timer", 0))
    current = read_sleep_timer(dev)
    if want:
        got = set_sleep_timer(dev, want, log=log)
        log(f"[{stamp()}] panel sleep timer set to {got} min")
    elif current is not None:
        log(f"[{stamp()}] panel sleeps after {current} min idle (dim_timer to change)")
    if cfg.num("dim_after", 0.0):
        log(
            "note: dim_after has no effect over USB -- nothing registers here, so "
            "the panel follows its own sleep timer. Use dim_timer instead."
        )
    auto = cfg.get("transport", "auto").strip().lower() not in ("usb", "network")
    poll_ms = cfg.num("poll_usb", 50.0)
    rearm = cfg.num("rearm", 300.0)
    log(f"[{stamp()}] watching for presses every {poll_ms:.0f}ms")

    was_pressed = False
    lost = 0
    next_rearm = time.monotonic() + rearm if rearm else None
    next_keepalive = time.monotonic() + KEEPALIVE_S
    keepalive = is_ix1600(dev)
    try:
        while True:
            try:
                g, _ = dev.hw_status(0x30)
                lost = 0
            except Exception:  # noqa: BLE001 -- transient USB hiccup
                # Cycling the ADF cover powers the scanner off, and it comes
                # back with a NEW usb device number, leaving the old handle
                # permanently dead. Without reconnecting, the daemon stays
                # alive but deaf: it sits here and never sees another press.
                lost += 1
                if lost % 6 == 0:
                    try:
                        dev = Ix1500()
                        arm(dev, user_id, log=log)
                        log(f"[{stamp()}] scanner re-attached; re-armed")
                        lost = 0
                    except ScannerAbsent:
                        # Unplugged, or the ADF cover was closed. If the
                        # transport was chosen automatically, the network path
                        # may well be available now -- the cable is exactly what
                        # was blocking it. Exit so the service restarts and
                        # re-resolves; staying here spins silently and leaves
                        # the panel on its error screen with nobody registering.
                        if lost >= 60 and auto:
                            log(
                                f"[{stamp()}] scanner has been off USB for 30s; "
                                "exiting so the transport is chosen again"
                            )
                            return 75  # EX_TEMPFAIL: systemd restarts us
                        if lost == 6:
                            log(f"[{stamp()}] scanner is not on USB; waiting")
                    except Exception:  # noqa: BLE001 -- busy, or mid-reset
                        pass
                time.sleep(0.5)
                continue
            if len(g) < 8:
                time.sleep(0.5)
                continue

            pressed = bool(g[4] & SCAN_SW_MASK)
            if pressed and not was_pressed:
                paper = not bool(g[3] & HOPPER_EMPTY_MASK)
                log(f"[{stamp()}] SCAN BUTTON PRESSED (paper_loaded={paper})")
                scan_once(cfg, dev, log=log)
                was_pressed = False
                next_keepalive = time.monotonic() + KEEPALIVE_S
                continue
            was_pressed = pressed

            if next_rearm and time.monotonic() >= next_rearm:
                arm(dev, user_id, log=log)
                next_rearm = time.monotonic() + rearm
                next_keepalive = time.monotonic() + KEEPALIVE_S
            elif keepalive and time.monotonic() >= next_keepalive:
                try:
                    open_session(dev, user_id)
                except Exception as exc:  # noqa: BLE001 -- the poll handles a lost device
                    log(f"[{stamp()}] session keep-alive failed: {exc}")
                next_keepalive = time.monotonic() + KEEPALIVE_S
            time.sleep(poll_ms / 1000.0)
    except KeyboardInterrupt:
        log("")
    return 0


def scan_once(cfg: Config, dev: Ix1500, log=print) -> None:
    """Scan the hopper, then hand the pages to a worker to assemble and name.

    The scan itself is synchronous: the scanner is waiting on us and a second
    press mid-scan would only confuse it. Post-processing is not -- OCR and a
    rename hook can take minutes, and the panel should be usable again as soon
    as the paper has gone through.
    """
    work = Path(tempfile.mkdtemp(prefix="panelbeater-"))
    failed = False
    try:
        scanner = UsbScanner(
            dev,
            resolution=int(cfg.num("resolution", 300)),
            mode=cfg.get("mode", "color"),
            duplex=not cfg.flag("simplex"),
        )
        sides = scanner.scan_batch(str(work / "page"), int(cfg.num("max_sheets", 100)))
        log(f"[{stamp()}] {sides} side(s) scanned")
    except Exception as exc:  # noqa: BLE001
        log(f"[{stamp()}] scan failed: {exc}")
        sides = 0
        failed = True
    # Return the panel to the profile screen after a failure -- a crashed scan
    # is exactly when it would otherwise stick on "Scanning...". On an iX1600
    # not after a good batch: scan_batch has already ended it, and a second
    # 31 02 after the last sheet fails and holds the panel on "Scanning...".
    if failed or not is_ix1600(dev):
        finish_batch(dev, log=log)

    pages = sorted(str(p) for p in work.glob("page-*.jpg")) if sides else []
    if not pages:
        shutil.rmtree(work, ignore_errors=True)
        return

    def finish():
        try:
            output.finish(pages, time.strftime("%Y%m%d-%H%M%S"), cfg, log=log)
        except Exception as exc:  # noqa: BLE001 -- a worker must not die silently
            log(f"  post-processing failed: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    threading.Thread(target=finish).start()
