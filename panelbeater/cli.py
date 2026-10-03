# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from . import output
from .config import Config, config_paths, derive_host_id
from .enrol import enrol
from .protocol import discover, hw_status, local_ip_and_mac
from .scanning import scan_to_dir
from .session import Session


def usb_present() -> bool:
    """Is the scanner on the USB bus? Checked via sysfs, so it costs nothing
    and does not need pyusb or a claim on the device."""
    from pathlib import Path as _P

    for f in _P("/sys/bus/usb/devices").glob("*/idProduct"):
        try:
            if f.read_text().strip() != "159f":
                continue
            if (f.parent / "idVendor").read_text().strip() == "04c5":
                return True
        except OSError:
            continue
    return False


def pick_transport(cfg: Config, override: str = "") -> str:
    """network or usb. `auto` prefers USB, which needs no enrolment."""
    want = (override or cfg.get("transport", "auto")).strip().lower()
    if want in ("network", "usb"):
        return want
    from . import usb as usbmod

    if usb_present() and usbmod.available():
        return "usb"
    if usb_present():
        print("scanner is on USB but pyusb is not installed; using the network",
              file=sys.stderr)  # fmt: skip
    return "network"


def find_scanner(cfg: Config, override: str = "") -> str | None:
    """The scanner's address: the configured one, else whatever answers
    discovery. None when nothing answers -- the scanner is off, or elsewhere."""
    addr = (override or cfg.get("scanner", "auto")).strip()
    if addr and addr.lower() != "auto":
        return addr
    found = discover()
    if not found:
        return None
    if len(found) > 1:
        print(f"several scanners answered: {found}; using {found[0]}", file=sys.stderr)
    return found[0]


def resolve_scanner(cfg: Config, override: str = "") -> str:
    print("looking for a scanner...", file=sys.stderr)
    host = find_scanner(cfg, override)
    if host is None:
        print(
            "no scanner found. Set `scanner = <ip>` in the config, or check the\n"
            "scanner is on the network (its panel shows the address under\n"
            "Settings -> Wi-Fi).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return host


# serve: how long to wait between looks for a switched-off scanner. USB is
# checked every second throughout (a sysfs read); only the network look, a
# broadcast and a subnet sweep, backs off.
WAIT_FIRST = 5.0
WAIT_MAX = 60.0


def wait_for_scanner(cfg: Config, args, sleep=time.sleep) -> tuple[str, str]:
    """Block until a scanner is reachable. Returns (transport, host).

    A scanner that is switched off is the normal state of a machine that has
    one, not an error, so serve keeps looking rather than exiting for
    systemd to restart it: a restart loop buries the journal in failures and
    reports a config-error exit status for something that needs no fixing.
    """
    want = (args.transport or cfg.get("transport", "auto")).strip().lower()
    waited = False
    delay = WAIT_FIRST
    while True:
        usb_seen = usb_present()
        if want != "network" and usb_seen:
            # pick_transport also falls back to the network when pyusb is
            # missing, and says so.
            transport = pick_transport(cfg, args.transport)
            if transport == "usb":
                return transport, ""
        if want != "usb":
            host = find_scanner(cfg, args.scanner)
            if host:
                return "network", host
        if not waited:
            print(
                "no scanner on USB or the network; waiting for one to be switched on",
                file=sys.stderr, flush=True,
            )  # fmt: skip
            waited = True
        # Look at USB each second, so a scanner switched on over the cable is
        # picked up at once, then at the network again after `delay`.
        for _ in range(int(delay)):
            sleep(1)
            if want != "network" and not usb_seen and usb_present():
                break
        delay = min(delay * 2, WAIT_MAX)


def cmd_discover(args, cfg: Config) -> int:
    found = discover(timeout=args.timeout)
    if not found:
        print("no scanners answered")
        return 1
    for ip in found:
        print(ip)
    return 0


def cmd_status(args, cfg: Config) -> int:
    host = resolve_scanner(cfg, args.scanner)
    _, mac, _ = local_ip_and_mac(host)
    print(f"scanner:   {host}")
    print(f"host_id:   {cfg.host_id}{'  (derived)' if not cfg.get('host_id') else ''}")
    print(f"name:      {cfg.name}")
    print(f"config:    {cfg.source or 'none found, using defaults'}")
    print(f"output:    {cfg.output_dir}")
    print(f"staging:   {cfg.staging_dir}")
    print(f"hook:      {cfg.get('hook') or '(none)'}")
    try:
        g = hw_status(host, mac)
        print(f"hopper:    {'paper loaded' if not (g[3] & 0x80) else 'empty'}")
        print(f"asleep:    {bool(g[4] & 0x80)}")
    except OSError as exc:
        print(f"hardware:  unreachable ({exc})")
    s = Session(host, cfg.host_id)
    doc = s.host_list()
    users = doc.get("conn_user", {}).get("users", [])
    sel = (doc.get("conn_user", {}).get("select") or {}).get("id")
    if users:
        print("hosts known to the scanner:")
        for u in users:
            mark = "  <- selected on the panel" if u.get("host_id") == sel else ""
            mine = "  (this machine)" if u.get("host_id") == cfg.host_id else ""
            print(f"    {u.get('host_id')}  {u.get('name')!r}{mine}{mark}")
        if cfg.host_id not in [u.get("host_id") for u in users]:
            print("\nthis machine is NOT enrolled; run:  panelbeater enrol")
    profs = doc.get("profiles", [])
    if profs:
        print(f"profiles:  {', '.join(repr(p.get('prof_name')) for p in profs)}")
    return 0


def cmd_enrol(args, cfg: Config) -> int:
    host = resolve_scanner(cfg, args.scanner)
    ok = enrol(
        host,
        cfg.host_id,
        args.name or cfg.name,
        dry_run=args.dry_run,
        via=args.via,
    )
    if ok and not args.dry_run:
        print(
            "\nOn the scanner's panel, tap the host name at the top and choose\n"
            f"{args.name or cfg.name!r} to point the panel at this machine.\n"
            "The panel decides that, not the host -- writes to it are ignored."
        )
    return 0 if ok else 1


def cmd_scan(args, cfg: Config) -> int:
    if pick_transport(cfg, args.transport) == "usb":
        return scan_over_usb(cfg)
    host = resolve_scanner(cfg, args.scanner)
    work = Path(tempfile.mkdtemp(prefix="panelbeater-"))
    try:
        n = scan_to_dir(
            host,
            cfg.host_id,
            str(work / "page"),
            prof_id=args.prof_id or cfg.get("prof_id"),
            max_sheets=args.max_sheets or int(cfg.num("max_sheets", 100)),
            skip_register=args.skip_register,
        )
        if not n:
            return 1
        print(f"{n} side(s) scanned")
        pages = sorted(str(p) for p in work.glob("page-*.jpg"))
        final = output.finish(pages, time.strftime("%Y%m%d-%H%M%S"), cfg)
        if final:
            print(final)
        return 0
    finally:
        for p in work.glob("*"):
            p.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass


def scan_over_usb(cfg: Config) -> int:
    """One scan over USB, arming the panel first so the scanner is willing."""
    from .usb.daemon import arm, scan_once, user_id_from_scanner
    from .usb.panel import Panel
    from .usb.transport import Ix1500, ScannerAbsent, ScannerBusy

    try:
        dev = Ix1500()
    except (ScannerAbsent, ScannerBusy) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    uid = cfg.get("user_id") or user_id_from_scanner(Panel(dev))
    if uid:
        arm(dev, uid)
    scan_once(cfg, dev)
    return 0


# A scanner that has just been switched on enumerates on USB several seconds
# before it answers, so the first start can fail through no fault of the
# config. Try a few times before giving up.
USB_START_TRIES = 11
USB_START_DELAY = 3.0
USB_START_SETTLED = 10.0  # a run longer than this was not a failed start
TRY_ENV = "PANELBEATER_START_TRY"


def restart_process(sleep=time.sleep) -> None:
    """Start over as a fresh process, keeping the PID. Returns only when it
    cannot.

    A failed USB start leaves this process unable to claim the scanner again:
    every retry in-process fails with "claimed by another process", while a
    new process claims it at once (seen on an iX1600, 2026-10-03). So the
    retry replaces the process rather than looping inside it.
    """
    argv = getattr(sys, "orig_argv", None)
    if not argv:
        return
    sleep(USB_START_DELAY)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


def cmd_serve(args, cfg: Config) -> int:
    while True:
        transport, host = wait_for_scanner(cfg, args)
        if transport == "usb":
            from .usb.daemon import serve as usb_serve

            print("transport: usb", flush=True)
            began = time.monotonic()
            rc = usb_serve(cfg)
            if rc == 75:  # off USB for good; choose the transport again
                os.environ.pop(TRY_ENV, None)
                restart_process(sleep=lambda _: None)
                return rc
            if rc == 1 and time.monotonic() - began < USB_START_SETTLED:
                tries = int(os.environ.get(TRY_ENV, "0")) + 1
                if tries < USB_START_TRIES:
                    os.environ[TRY_ENV] = str(tries)
                    print(
                        f"scanner not ready; trying again ({tries}/{USB_START_TRIES - 1})",
                        flush=True,
                    )  # fmt: skip
                    restart_process()
            return rc
        from .daemon import serve

        print("transport: network", flush=True)
        return serve(cfg, host)


def cmd_config(args, cfg: Config) -> int:
    if args.show:
        print(f"# loaded from: {cfg.source or '(defaults only)'}")
        print("[panelbeater]")
        for k in sorted(cfg.values):
            print(f"{k} = {cfg.values[k]}")
        return 0
    target = Path(args.write) if args.write else config_paths()[0]
    if target.exists() and not args.force:
        print(f"{target} exists; pass --force to overwrite")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    from .config import DEFAULTS

    lines = [
        "# panelbeater configuration",
        "# Every key can also be set as PANELBEATER_<KEY> in the environment.",
        "[panelbeater]",
    ]
    for k, v in DEFAULTS.items():
        if k == "host_id" and not v:
            lines.append(
                f"# host_id = {derive_host_id()}   # derived from this machine"
            )
            continue
        lines.append(f"{k} = {v}")
    target.write_text("\n".join(lines) + "\n")
    print(f"wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="panelbeater",
        description="Scan from a ScanSnap iX1500's own touch panel, on Linux.",
    )
    ap.add_argument("--config", help="config file to use")
    ap.add_argument(
        "--scanner", default="", help="scanner IP (default: config, or discover)"
    )
    ap.add_argument(
        "--transport",
        default="",
        choices=["", "network", "usb"],
        help="override the configured transport",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="find scanners on the network")
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("status", help="show configuration and what the scanner knows")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("enrol", help="add this machine to the scanner's host list")
    p.add_argument("--name", default="", help="how this host appears on the panel")
    p.add_argument(
        "--via", default="", help="register as this already-enrolled host_id"
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_enrol)
    sub._name_parser_map["enroll"] = p  # US spelling

    p = sub.add_parser("scan", help="scan whatever is in the hopper, now")
    p.add_argument("--prof-id", default="")
    p.add_argument("--max-sheets", type=int, default=0)
    p.add_argument("--skip-register", action="store_true",
                   help="a running `panelbeater serve` already holds the registration")  # fmt: skip
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("serve", help="keep the panel alive and scan on button press")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("config", help="show or create the configuration file")
    p.add_argument("--show", action="store_true", help="print the effective settings")
    p.add_argument("--write", default="", help="path to write a starter config to")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_config)

    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"panelbeater: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
