# SPDX-License-Identifier: GPL-2.0-or-later
import pytest

from panelbeater import scanning
from panelbeater.scanning import D4_PARAMS, scan_batch, scan_params

IMAGE = b"\x00\x00\xff\xd8\xff" + bytes(20000) + b"\xff\xd9"


class FakeScanner:
    """Answers at the scsi() boundary: every read is an image, sense is clean."""

    host, mac = "scanner", bytes(6)

    def __init__(self):
        self.d4 = None

    def scsi(self, cdb, read_len=0, out=b"", collect=False, quiet=6.0):
        if cdb[0] == 0xD4:
            self.d4 = out
        if cdb[0] == 0x28 and cdb[2] == 0x00:
            return 0, IMAGE
        if cdb[0] == 0x03:
            return 0, bytes(18)
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
    sheets = iter([True, False])  # paper after sheet 1, then empty
    monkeypatch.setattr(scanning, "hopper_has_paper", lambda host, mac: next(sheets))
    s = FakeScanner()
    params = scan_params(200, "gray", log=lambda m: None)
    n = scan_batch(s, str(tmp_path / "page"), params=params, simplex=True, log=lambda m: None)
    assert s.d4 == params
    assert n == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page-0001.jpg", "page-0002.jpg"]


@pytest.fixture
def sleepless(monkeypatch):
    monkeypatch.setattr(scanning.time, "sleep", lambda s: None)
