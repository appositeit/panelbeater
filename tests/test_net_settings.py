# SPDX-License-Identifier: GPL-2.0-or-later
import pytest

from panelbeater import scanning
from panelbeater.scanning import D4_PARAMS, scan_batch, scan_params

IMAGE = b"\x00\x00\xff\xd8\xff" + bytes(20000) + b"\xff\xd9"


class FakeScanner:
    """Answers at the scsi() boundary with `sheets` sheets, then ends the batch
    the way the hardware does: 34 zero bytes and sense 05/2C/02."""

    host, mac = "scanner", bytes(6)

    def __init__(self, sheets):
        self.sheets, self.fed, self.d4, self.sense = sheets, 0, None, bytes(18)

    def scsi(self, cdb, read_len=0, out=b"", collect=False, quiet=6.0):
        if cdb[0] == 0xD4:
            self.d4 = out
        if cdb[0] == 0xE0:
            self.fed += 1
        if cdb[0] == 0x28 and cdb[2] == 0x00:
            if self.fed > self.sheets:
                self.sense = bytes([0, 0, 0x05] + [0] * 9 + [0x2C, 0x02, 0, 0, 0, 0])
                return 0, bytes(34)
            return 0, IMAGE
        if cdb[0] == 0x03:
            return 0, self.sense
        return 0, b""


@pytest.mark.parametrize(
    "dpi, mode, xres, comp",
    [(150, "color", 150, 0x05), (200, "gray", 200, 0x02), (300, "color", 300, 0x05),
     (600, "color", 300, 0x05), (300, "lineart", 300, 0x02)],  # fmt: skip
)
def test_d4_fields(dpi, mode, xres, comp):
    p = scan_params(dpi, mode, log=lambda m: None)
    assert p[0x22:0x26] == xres.to_bytes(2, "big") * 2
    assert p[0x26] == comp
    # Nothing else in the captured block moves.
    assert p[:0x22] + p[0x27:] == D4_PARAMS[:0x22] + D4_PARAMS[0x27:]


def test_default_is_the_captured_block():
    assert scan_params(log=lambda m: None) == D4_PARAMS


def test_settings_reach_the_scanner_and_simplex_drops_backs(tmp_path, monkeypatch, sleepless):
    s = FakeScanner(sheets=2)
    monkeypatch.setattr(scanning, "hopper_has_paper", lambda host, mac: s.fed < s.sheets)
    params = scan_params(200, "gray", log=lambda m: None)
    n = scan_batch(s, str(tmp_path / "page"), params=params, simplex=True, log=lambda m: None)
    assert s.d4 == params
    assert n == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page-0001.jpg", "page-0002.jpg"]


@pytest.fixture
def sleepless(monkeypatch):
    monkeypatch.setattr(scanning.time, "sleep", lambda s: None)
