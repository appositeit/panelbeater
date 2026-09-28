# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scanning over USB, without SANE.

The USB path needs no enrolment and no host_id: `SEND DIAGNOSTIC` has no
registration concept, so it works on a scanner straight out of the box. That
makes it the easier path to hand to somebody else, and the only one available
if the scanner is not on Wi-Fi.

It is also mutually exclusive with SANE, in both directions. Only one process
can claim the USB interface, so while panelbeater is running `scanimage` fails
at open with "Invalid argument", and vice versa. Scanning is therefore done
in-process rather than by shelling out to scanimage: handing the device over
for the duration of a scan means being blind to the Stop button, blind to
errors, and inferring "finished" from an exit code.

  transport.py  the Fujitsu SCSI-over-USB envelope
  panel.py      the SETUP PROF INFO document channel
  sequence.py   the captured setup commands, byte for byte
  scanner.py    running a scan and decoding the image
  daemon.py     arm the panel, watch the button, scan
"""

__all__ = ["Ix1500", "Panel", "PanelError", "ScannerAbsent", "ScannerBusy"]


def __getattr__(name: str):
    """Import the pyusb-backed classes on first use, not at package import.

    `transport = auto` imports this package just to call available(), so an
    eager `import usb.core` here crashed the daemon on every network-only
    machine without pyusb -- the path the README promises is stdlib-only.
    """
    if name in ("Panel", "PanelError"):
        from . import panel

        return getattr(panel, name)
    if name in ("Ix1500", "ScannerAbsent", "ScannerBusy"):
        from . import transport

        return getattr(transport, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def available() -> bool:
    """Is pyusb installed? The USB path is optional."""
    try:
        import usb.core  # noqa: F401
    except ImportError:
        return False
    return True
