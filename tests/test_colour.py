# SPDX-License-Identifier: GPL-2.0-or-later
import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

from panelbeater import colour, output  # noqa: E402
from panelbeater.config import DEFAULTS, Config  # noqa: E402

DPI = 150
A4 = (round(8.27 * DPI), round(11.69 * DPI))


def quiet(msg):
    pass


def fringed_text(path, stamp=False):
    """Black strokes whose R and B are read half a pixel off G: coloured edges."""
    rng = np.random.default_rng(0)
    im = Image.new("L", A4, 240)
    d = ImageDraw.Draw(im)
    for y in range(100, A4[1] - 100, 26):
        for x in range(100, A4[0] - 125, 15):
            for _ in range(3):
                x0, y0, x1, y1 = rng.integers(0, 12, 4)
                d.line((x + x0, y + y0, x + x1, y + y1 + 4), fill=20, width=2)
    g = np.asarray(im, dtype=np.float32)
    r = (g + np.roll(g, 1, axis=0)) / 2
    b = (g + np.roll(g, -1, axis=0)) / 2
    rgb = Image.fromarray(np.dstack([r, g, b]).round().astype(np.uint8))
    if stamp:  # a red ring 15 mm across
        ring = (1000, 1500, 1088, 1588)
        ImageDraw.Draw(rgb).ellipse(ring, outline=(200, 40, 40), width=5)
    rgb.save(path, "JPEG", quality=75, dpi=(DPI, DPI))
    return str(path)


def test_colour_fringes_around_black_text_become_gray(tmp_path):
    p = fringed_text(tmp_path / "page-0001.jpg")
    with Image.open(p) as im:
        a = np.asarray(im, dtype=np.int16)
    assert (a.max(axis=2) - a.min(axis=2)).max() > colour.CHROMA  # fringes exist

    assert colour.reduce_pages([p], "auto", log=quiet) == [p]
    with Image.open(p) as im:
        assert im.mode == "L"
        assert im.info["dpi"] == (DPI, DPI)


def test_a_small_coloured_mark_keeps_the_page_colour(tmp_path):
    p = fringed_text(tmp_path / "page-0001.jpg", stamp=True)
    assert colour.reduce_pages([p], "auto", log=quiet) == [p]
    with Image.open(p) as im:
        assert im.mode == "RGB"


def test_lineart_is_one_bit_png_at_the_scan_dpi(tmp_path):
    rng = np.random.default_rng(0)
    paper = rng.normal(215, 4, (800, 600)).clip(0, 255).astype(np.uint8)
    im = Image.fromarray(paper)
    ImageDraw.Draw(im).rectangle((100, 100, 500, 130), fill=50)  # dark text
    src = tmp_path / "work" / "page-0001.jpg"
    src.parent.mkdir()
    im.convert("RGB").save(src, "JPEG", quality=75, dpi=(200, 200))

    cfg = Config({**DEFAULTS, "mode": "lineart", "pdf": "no", "blank_removal": "no",
                  "output_dir": str(tmp_path / "out")})  # fmt: skip
    out = output.finish([str(src)], "stamp", cfg, log=quiet)

    assert out.suffix == ".png"
    with Image.open(out) as bw:
        assert bw.mode == "1"
        assert tuple(round(v) for v in bw.info["dpi"]) == (200, 200)
        a = np.asarray(bw)
    assert not a[105:126, 105:496].any()  # text black
    assert a[300:, :].all()  # paper white


def test_lineart_leaves_a_blank_side_white(tmp_path):
    rng = np.random.default_rng(0)
    paper = rng.normal(235, 3, (400, 300)).clip(0, 255).astype(np.uint8)
    src = tmp_path / "page-0001.jpg"
    Image.fromarray(paper).save(src, "JPEG", quality=75, dpi=(300, 300))
    with Image.open(colour.to_lineart(str(src))) as bw:
        assert np.asarray(bw).all()


def test_img2pdf_takes_jpeg_and_one_bit_png(tmp_path):
    pytest.importorskip("img2pdf")
    jpg = fringed_text(tmp_path / "page-0001.jpg")
    png = colour.to_lineart(fringed_text(tmp_path / "page-0002.jpg"))
    logs = []
    assert output.assemble_pdf([jpg, png], tmp_path / "doc.pdf", log=logs.append)
    assert logs == []  # img2pdf did it, not the Pillow fallback
    assert b"/BitsPerComponent 1" in (tmp_path / "doc.pdf").read_bytes()
