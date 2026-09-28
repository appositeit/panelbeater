# SPDX-License-Identifier: GPL-2.0-or-later
import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

from panelbeater.blank import DEFAULT_THRESHOLD, max_block_ink  # noqa: E402


def page(tmp_path, dpi, marks):
    """An A4-ish white page with dark rectangles at (x, y, w, h) in mm."""
    px = lambda mm: round(mm * dpi / 25.4)  # noqa: E731
    a = np.full((px(297), px(221)), 235, dtype=np.uint8)
    for x, y, w, h in marks:
        a[px(y) : px(y + h), px(x) : px(x + w)] = 40
    path = tmp_path / f"page-{dpi}.jpg"
    Image.fromarray(a).save(path, dpi=(dpi, dpi), quality=95)
    return str(path)


def test_punch_hole_shadows_are_not_ink(tmp_path):
    # Shadows inside two DIN punch holes, as measured on an iX1500 back side.
    holes = [(14, 109, 5.5, 1.3), (14, 195, 5.5, 1.3)]
    assert max_block_ink(page(tmp_path, 300, holes)) < DEFAULT_THRESHOLD


def test_single_small_letter_at_150_dpi_is_ink(tmp_path):
    # About 60 px of ink. Blocks sized for 300 dpi are five times the area
    # at 150 dpi and dilute it below the threshold.
    letter = [(100, 140, 1, 1.7)]
    assert max_block_ink(page(tmp_path, 150, letter)) >= DEFAULT_THRESHOLD
