# SPDX-License-Identifier: GPL-2.0-or-later
import pytest

from panelbeater import output
from panelbeater.config import DEFAULTS, Config

STAMP = "20260928-120000"


def config(tmp_path, **values):
    return Config({**DEFAULTS, "output_dir": str(tmp_path / "out"), **values})


def scan(tmp_path, stamp, n):
    """A work dir of n sides named as scan_batch names them."""
    work = tmp_path / stamp
    work.mkdir()
    for i in range(1, n + 1):
        (work / f"page-{i:04d}.jpg").write_text(f"{stamp}/{i}")
    return sorted(str(p) for p in work.iterdir())


def test_jpegs_without_a_pdf_are_named_by_scan(tmp_path):
    cfg = config(tmp_path, pdf="no", blank_removal="no")
    for stamp in (STAMP, "20260928-120100"):
        output.finish(scan(tmp_path, stamp, 2), stamp, cfg, log=lambda m: None)
    assert {p.name: p.read_text() for p in cfg.output_dir.glob("*.jpg")} == {
        "scan-20260928-120000-001.jpg": "20260928-120000/1",
        "scan-20260928-120000-002.jpg": "20260928-120000/2",
        "scan-20260928-120100-001.jpg": "20260928-120100/1",
        "scan-20260928-120100-002.jpg": "20260928-120100/2",
    }


def assemble(pages, out, log):
    out.write_bytes(b"%PDF")
    return True


@pytest.mark.parametrize("lang", ["deu+eng", ""])
def test_ocr_lang_reaches_ocrmypdf(tmp_path, monkeypatch, lang):
    calls = []
    monkeypatch.setattr(output, "assemble_pdf", assemble)
    monkeypatch.setattr(output, "_which", lambda *names: "/usr/bin/" + names[0])
    monkeypatch.setattr(output.subprocess, "run", lambda args, **kw: calls.append(args))
    cfg = config(tmp_path, blank_removal="no", ocr_lang=lang)
    output.finish(scan(tmp_path, STAMP, 1), STAMP, cfg, log=lambda m: None)
    [args] = calls
    assert args[0] == "/usr/bin/ocrmypdf"
    if lang:
        assert args[args.index("-l") + 1] == lang
    else:
        assert "-l" not in args
