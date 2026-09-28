# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Decide which scanned sides are blank, so a duplex scan of one-sided paper
does not produce a document that is half empty.

sane-backends already does this -- the fujitsu backend's `swskip` option, via
sanei_magic_isBlank2() -- but scanning over the network means none of that code
is in our path. This borrows its good idea and drops the part that does not
survive real ScanSnap output.

The good idea is that the test must be LOCAL. sanei_magic_isBlank2 splits the
page into half-inch blocks and keeps the page if any single block is dark
enough. A whole-page average dilutes a signature or a corner stamp into
nothing; a block test catches it.

Where it differs: SANE measures mean darkness, paper background included. On
real iX1500 scans the paper is not white, and that swamps the signal --

                        SANE block darkness        block ink fraction
                        max      median            max      median
    page with text    20.539%    4.447%          17.221%    0.000%
    blank reverse      7.885%    4.687%           0.000%    0.000%

The paper alone accounts for ~4.5% darkness on both, so the blank page's
darkest block outranks the text page's median. A threshold has to thread
between 7.9 and 20.5, and greyer paper closes the gap. Counting the fraction of
pixels that are actually ink removes the background term: a blank side measures
exactly zero.

Punch holes are the exception. The shadow inside a hole reads as ink, 1.4-1.9%
of its block, which is more than the threshold and about what a pencil drawing
scores, so no threshold separates them. The holes sit 14-20 mm in from the
side of the scan, so the sides are skipped further than the top and bottom.

Requires Pillow and numpy. Without them blank removal is skipped and every side
is kept, which is the safe direction.
"""

from __future__ import annotations

from typing import Callable, Sequence

# Percent of a single block that must be ink for the page to count as content.
# A block is half an inch square: 144x144 = 20,736 px at 300 dpi. One character
# of 300 dpi text fills roughly 1.2% of that, so this catches a page bearing a
# single character, while a 10x10 dust speck (0.48%) does not trip it.
DEFAULT_THRESHOLD = 0.5
DARK = 128  # a pixel this dark or darker is ink
SIDE_MM = 22  # skipped at the left and right: past the punch holes


def available() -> bool:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def max_block_ink(path: str, dark: int = DARK) -> float:
    """Ink percentage of the densest half-inch block on the page."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        dpi = int(round(im.info.get("dpi", (300, 300))[0])) or 300
        a = np.asarray(im.convert("L"), dtype=np.uint8)

    quarter = dpi // 4 // 8 * 8  # top and bottom border to ignore, as sanei_magic does
    side = round(SIDE_MM * dpi / 25.4)
    half = quarter * 2  # block edge: half an inch
    h, w = a.shape
    yb, xb = (h - half) // half, (w - 2 * side) // half
    if yb < 1 or xb < 1:  # page smaller than one block: measure it whole
        return 100.0 * float((a < dark).mean())

    core = a[quarter : quarter + yb * half, side : side + xb * half]
    blocks = core.reshape(yb, half, xb, half)
    return 100.0 * float((blocks < dark).mean(axis=(1, 3)).max())


def keep_pages(
    paths: Sequence[str],
    threshold: float = DEFAULT_THRESHOLD,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Which of these sides are worth keeping. Never returns an empty list."""
    if not available():
        log("  blank removal needs Pillow and numpy; keeping every side")
        return list(paths)

    scored: list[tuple[str, float]] = []
    for p in paths:
        try:
            scored.append((p, max_block_ink(p)))
        except Exception as exc:  # noqa: BLE001 -- an unreadable page is kept
            log(f"  {p}: unreadable ({exc}) -- keeping")
            scored.append((p, float("inf")))

    keep = [(p, v) for p, v in scored if v >= threshold]

    # Everything looked blank: the threshold is wrong, or the scan failed.
    # Dropping the lot would throw the document away without saying so.
    if scored and not keep:
        log(f"  all {len(scored)} side(s) look blank -- keeping them all")
        keep = list(scored)

    kept = {p for p, _ in keep}
    for p, v in scored:
        shown = "unreadable" if v == float("inf") else f"{v:7.3f}% ink"
        log(f"  {'keep ' if p in kept else 'BLANK'} {shown}  {p}")
    return [p for p, _ in keep]
