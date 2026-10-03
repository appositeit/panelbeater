# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scan from a ScanSnap iX1500's own touch panel, on Linux, without vendor software."""

__version__ = "0.1.0"

# USB IDs of the models known to speak this protocol. Plain data, so that the
# sysfs checks in cli.py and daemon.py can use it without importing pyusb.
USB_VENDOR = 0x04C5
USB_PRODUCTS = (
    0x159F,  # iX1500
    0x1632,  # iX1600
)
