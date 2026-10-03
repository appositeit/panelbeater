# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Configuration: an INI file, overridable by environment variables.

Everything machine-specific lives here, so the code itself carries no paths,
addresses or identifiers belonging to whoever happened to write it.

Search order, first match wins:

    $PANELBEATER_CONFIG
    $XDG_CONFIG_HOME/panelbeater/config   (default ~/.config/panelbeater/config)
    /etc/panelbeater/config

Any setting can be overridden with PANELBEATER_<KEY> in the environment, which is
what makes the systemd unit configurable without editing it.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import socket
from pathlib import Path

SECTION = "panelbeater"

DEFAULTS: dict[str, str] = {
    # network, usb, or auto. USB needs no enrolment and no Wi-Fi, but it is
    # mutually exclusive with SANE: only one process can claim the interface,
    # so scanimage cannot open the scanner while panelbeater is running.
    # `auto` uses USB if the cable is there and pyusb is installed, else the
    # network.
    "transport": "auto",
    # Scanner address. "auto" broadcasts for it, which is slower but survives
    # DHCP moving the scanner.
    "scanner": "auto",
    # 16 hex characters. Blank derives a stable one from this machine.
    "host_id": "",
    # How this host appears in the scanner's panel list.
    "name": "",
    # Finished documents are filed here.
    "output_dir": "~/Documents/Scans",
    # Assembled and named here first. MUST NOT be watched by whatever consumes
    # output_dir, and should be on the same filesystem so the handover is an
    # atomic rename. Blank means "a subdirectory of output_dir".
    "staging_dir": "",
    # Optional program run with the finished PDF as its only argument. See
    # hooks/README.md for the contract.
    "hook": "",
    "hook_timeout": "300",
    # Combine the scanned sides into one PDF. Without it you get JPEGs.
    "pdf": "yes",
    # Duplex scanning one-sided paper yields a blank reverse for every sheet.
    "blank_removal": "yes",
    "blank_threshold": "0.5",
    # Crop each side to the sheet. The scanner sends a fixed window wider and
    # longer than the paper, with the grey backing around it.
    "autocrop": "no",
    # Which of the scanner's profiles to scan with. Blank uses the first one.
    # `panelbeater status` lists them; the ids come from the scanner, not from here.
    "prof_id": "",
    # Safety cap on a single batch; the batch normally ends when the hopper
    # empties.
    "max_sheets": "100",
    # Seconds between registrations. The panel goes dead if nobody is
    # registered, so this is also the keep-alive.
    "interval": "15",
    # Minutes of idleness before the panel is allowed to go dark. 0 keeps it
    # lit for ever, which is what the vendor software does. A lit panel in a
    # bedroom is a nightlight nobody asked for, so set this if the scanner
    # lives somewhere you sleep. Touching the panel wakes it and the Scan
    # button works again within a poll.
    #
    # Note this is when we STOP REGISTERING, not when the screen goes off: the
    # scanner then takes about thirteen more minutes (776s, 777s and 883s
    # measured).
    "dim_after": "0",
    # The scanner's own sleep timer, in minutes: how long after the last
    # activity the backlight goes off. 0 leaves it alone. Minimum 2, maximum
    # 224, clamped silently by the scanner.
    #
    # USB ONLY. On the network the write is accepted and ignored, so this is
    # applied when panelbeater is running over USB and skipped otherwise. Out
    # of the box the scanner uses about 15 minutes.
    "dim_timer": "0",
    # Milliseconds between button polls (network).
    "poll": "200",
    # Milliseconds between button polls over USB. Much faster, because a press
    # can be visible for as little as one poll and a job left unanswered goes
    # stale.
    "poll_usb": "50",
    # Seconds between re-arming the panel over USB; 0 disables. Cheap insurance
    # in case the panel loses the session.
    "rearm": "300",
    # USB only: the network path is fixed at 300 dpi colour because its
    # parameter block is not fully decoded.
    "resolution": "300",
    "mode": "color",
    "simplex": "no",
    # The 32-hex id the panel acts as. Read from the scanner's profiles when
    # blank, which is almost always right.
    "user_id": "",
}


def config_paths() -> list[Path]:
    env = os.environ.get("PANELBEATER_CONFIG")
    if env:
        return [Path(env)]
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return [
        Path(xdg).expanduser() / "panelbeater" / "config",
        Path("/etc/panelbeater/config"),
    ]


def derive_host_id() -> str:
    """A stable 16-hex-char id for this machine.

    The host computes its own id; the scanner does not issue one. A factory
    reset followed by re-pairing reproduced the identical id, so ScanSnap Home
    derives it too. Deriving from machine-id keeps it stable across reboots and
    reinstalls.
    """
    seed = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        p = Path(path)
        try:
            if p.is_file():
                seed = p.read_text().strip()
                break
        except OSError:
            pass
    if not seed:
        seed = socket.gethostname()
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


class Config:
    def __init__(self, values: dict[str, str], source: Path | None = None):
        self.values = values
        self.source = source

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
        values = dict(DEFAULTS)
        found: Path | None = None
        candidates = [Path(path)] if path else config_paths()
        for p in candidates:
            if p.is_file():
                parser = configparser.ConfigParser()
                # Tolerate a bare key=value file with no [panelbeater] header.
                text = p.read_text()
                if f"[{SECTION}]" not in text:
                    text = f"[{SECTION}]\n" + text
                parser.read_string(text)
                values.update(dict(parser[SECTION]))
                found = p
                break
        # Environment always wins, so a unit file or a one-off run can override
        # without touching the config.
        for key in DEFAULTS:
            env = os.environ.get(f"PANELBEATER_{key.upper()}")
            if env is not None:
                values[key] = env
        return cls(values, found)

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def flag(self, key: str) -> bool:
        return self.values.get(key, "").strip().lower() in ("1", "yes", "true", "on")

    def num(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.values.get(key, ""))
        except ValueError:
            return default

    def path(self, key: str) -> Path:
        return Path(self.values.get(key, "")).expanduser()

    @property
    def host_id(self) -> str:
        return self.values.get("host_id") or derive_host_id()

    @property
    def name(self) -> str:
        return self.values.get("name") or socket.gethostname()

    @property
    def output_dir(self) -> Path:
        return self.path("output_dir")

    @property
    def staging_dir(self) -> Path:
        raw = self.values.get("staging_dir", "").strip()
        if raw:
            return Path(raw).expanduser()
        # Hidden and inside output_dir, so it is on the same filesystem by
        # construction and the handover stays an atomic rename. A leading dot
        # also keeps most folder-watchers from noticing it.
        return self.output_dir / ".staging"
