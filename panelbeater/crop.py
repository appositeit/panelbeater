# SPDX-License-Identifier: GPL-2.0-or-later
"""Crop each scanned side to the paper.

The scanner does not do this. It scans a fixed window wider and longer than the
paper, and ScanSnap Home finds the sheet on the host. Without that step an A4
sheet from an iX1600 arrives as 221 x 314 mm over the network, with grey
margins and a dark shadow line along three edges.

The sheet is found from two measured facts about that window:

* The backing plate is grey, a little darker than paper: about 239 against
  250 on white copier paper, 247 on off-white letterhead.
* The sheet casts a dark shadow line on its left, right and trailing edges.
  At the trailing edge it runs across the full width of the sheet. Over USB
  a second dark line follows, where the window ends.

Medians along rows and columns ignore the printing, as long as ink covers less
than half of a line across the page. The leading and side edges are where the
median first rises above the midpoint between backing and paper, after some
backing has been seen (the first few USB columns read pure white). The
trailing edge is the first shadow that is dark across the full width. Paper
as grey as the backing has no brightness edge; then the sides are the side
shadows and the top is the lit leading edge (shadow_edge). When no
edge is clear, the side is kept whole -- a margin is harmless, a cropped line
of text is not.

Requires Pillow and numpy. Without them every side is kept whole.
"""

from __future__ import annotations

from typing import Callable, Sequence

RUN = 8  # consecutive lines above the midpoint that count as paper
SHADOW = 30  # a pixel this much darker than the backing is shadow (or ink)
INSET = 4  # pixels shaved inside the detected edge, to drop the edge gradient
SKEW_BAND = 60  # rows (5 mm at 300 dpi) a slanted trailing shadow may span
SIDE_SHADOW = 15  # a side shadow dips at least this far below the backing
BRIGHT_EDGE = 8  # the lit leading edge reads at least this far above it
A4_MM = (210.0, 297.0)
SNAP_MM = 4.0  # a crop this close to A4 in both directions is A4


def shadow_edge(profile, backing: float, limit: int) -> int | None:
    """First index inside the side shadow among the first `limit`, or None.

    From the outside in: backing, the shadow darkening to its deepest point,
    then a sharp rise to paper. The edge is where the profile is back up to
    the backing level.
    """
    import numpy as np

    outer = profile[:limit]
    low = int(np.argmin(outer))
    if outer[low] > backing - SIDE_SHADOW:
        return None
    rise = np.where(profile[low:limit] >= backing - 3)[0]
    return low + int(rise[0]) if len(rise) else None


def paper_box(gray) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the sheet in a grayscale array, or None."""
    import numpy as np

    h, w = gray.shape
    rows_with_data = np.where((gray < 250).any(axis=1))[0]
    if len(rows_with_data) == 0:
        return None
    extent = int(rows_with_data.max()) + 1  # past this, the feed ran on empty

    band = gray[int(extent * 0.15) : int(extent * 0.55)]
    cols = np.median(band, axis=0)
    backing = float(np.median(np.concatenate([cols[8:24], cols[-24:-8]])))
    # A high percentile over the whole width, not the median of the centre:
    # print pulls the median towards the backing, and off-white paper can be
    # darker in the middle than at the edges. An off-white sheet read 244 in
    # the centre and 248 near the edges, against backing at 240, and was kept
    # whole. Clear of the outer 24 columns, where USB reads pure white.
    paper = float(np.percentile(cols[24:-24], 95))
    mid = (backing + paper) / 2

    def first_run(profile) -> int | None:
        above = profile > mid
        backing_seen = np.where(~above)[0]
        if len(backing_seen) == 0:
            return None
        for i in range(int(backing_seen[0]), len(profile) - RUN):
            if above[i : i + RUN].all():
                return i
        return None

    if paper - backing >= 5:
        left = first_run(cols)
        right_rev = first_run(cols[::-1])
    else:
        left = right_rev = None
    if left is None or right_rev is None:
        # Grey paper: recycled stock read 237-238 against backing at 239-240,
        # so brightness finds no edge. The side shadows still do -- each dips to
        # about 210 and the paper starts sharply after it. Where both methods
        # work they agree within a pixel.
        limit = int(w * 0.08)
        left = shadow_edge(cols, backing, limit)
        right_rev = shadow_edge(cols[::-1], backing, limit)
        if left is None or right_rev is None:
            return None
        rows = np.median(gray[:, w // 3 : 2 * w // 3], axis=1)
        # The leading edge catches the light: a band near 255 on every paper
        # measured. Within 5 rows of what brightness finds on white paper.
        lit = np.where(rows[: extent // 4] >= backing + BRIGHT_EDGE)[0]
        if len(lit) == 0:
            return None
        top = int(lit[0])
    else:
        top = None
    right = w - right_rev
    if right - left < w * 0.5:
        return None

    if top is None:
        span = right - left
        rows = np.median(gray[:, left + span // 4 : right - span // 4], axis=1)
        top = first_run(rows[:extent])
        if top is None:
            return None

    # The trailing shadow is dark across the whole sheet, margins included. A
    # ruled line or a grey band stops short of the margins and is passed over.
    # The first such line is the edge; over USB the end of the window follows.
    # A sheet fed at a slight angle slants the shadow over many rows, so look
    # for a band of SKEW_BAND rows with a dark pixel in nearly every column,
    # and put the edge at the lowest point of the shadow inside it.
    dark = gray[top:extent, left + 3 * INSET : right - 3 * INSET] < backing - SHADOW
    if len(dark) <= SKEW_BAND:
        return None
    cs = np.zeros((len(dark) + 1, dark.shape[1]), dtype=np.int32)
    np.cumsum(dark, axis=0, out=cs[1:])
    in_band = (cs[SKEW_BAND:] - cs[:-SKEW_BAND]) > 0
    hits = np.where(in_band.mean(axis=1) > 0.95)[0]
    hits = hits[hits > (extent - top) // 4]
    if len(hits) == 0:
        return None
    band = dark[hits[0] : hits[0] + SKEW_BAND]
    first_dark = band.argmax(axis=0)[band.any(axis=0)]
    bottom = top + int(hits[0]) + int(np.percentile(first_dark, 98))
    if bottom - top < extent * 0.3:
        return None

    return (left + INSET, top + INSET, right - INSET, bottom - INSET)


def crop_pages(
    pages: Sequence[str], dpi: int = 300, log: Callable[[str], None] = print
) -> None:
    """Crop each JPEG in place to its sheet. Never fails a scan."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        log("  autocrop skipped: needs Pillow and numpy")
        return
    for p in pages:
        try:
            with Image.open(p) as im:
                box = paper_box(np.asarray(im.convert("L")))
                if box is None:
                    log(f"  autocrop: no clear sheet edge, kept whole  {p}")
                    continue
                res = im.info.get("dpi", (dpi, dpi))
                res = (dpi, dpi) if round(res[0]) in (0, 72, 96) else res
                out = im.crop(box)
                out.load()
            # Edge detection and the inset leave an A4 sheet 1-2 mm off, so
            # the pages of one letter differed in size. Close enough is A4:
            # scaled (under 1%), not re-cropped, which would bring back a
            # grey line of backing along the edges.
            a4 = tuple(round(v / 25.4 * res[0]) for v in A4_MM)
            if all(abs(o - t) <= SNAP_MM / 25.4 * res[0] for o, t in zip(out.size, a4)):
                out = out.resize(a4, Image.LANCZOS)
            out.save(p, "JPEG", quality=90, dpi=res)
            mm = [round(v / res[0] * 25.4) for v in out.size]
            log(f"  autocrop: {mm[0]} x {mm[1]} mm  {p}")
        except (OSError, ValueError) as exc:
            log(f"  autocrop skipped for {p}: {exc}")
