# SPDX-License-Identifier: GPL-2.0-or-later
"""Reduce scanned sides to black and white or gray on this host.

Over the network the scanner only sends JPEG, so `lineart` is scanned gray and
thresholded here, and `auto` is scanned colour and every side without real
colour is turned gray. Requires Pillow and numpy; without them the sides are
kept as scanned.
"""

from __future__ import annotations

import os
from typing import Callable, Sequence

from .blank import DARK, available

# A side is colour if more than PERCENT of it has a chroma, max(R,G,B) -
# min(R,G,B), above CHROMA. Measured at 75 dpi after a one-pixel blur, which
# cancels the complementary fringes either side of a black stroke; a coloured
# mark survives it.
CHROMA = 32
PERCENT = 0.05
# Otsu classes closer together than this are paper noise, not ink on paper.
MIN_CONTRAST = 32


def colour_percent(path: str) -> float:
    """Percentage of the side that is clearly coloured."""
    import numpy as np
    from PIL import Image, ImageFilter

    with Image.open(path) as im:
        dpi = im.info.get("dpi", (300, 300))[0]
        small = im.convert("RGB").reduce(max(1, round(dpi / 75)))
    a = np.asarray(small.filter(ImageFilter.GaussianBlur(1)), dtype=np.int16)
    return 100.0 * float((a.max(axis=2) - a.min(axis=2) > CHROMA).mean())


def otsu(a) -> int:
    """Global threshold for an 8-bit array: values at or below it are ink."""
    import numpy as np

    hist = np.bincount(a.ravel(), minlength=256).astype(np.float64)
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m = np.cumsum(hist * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        mu0, mu1 = m / w0, (m[-1] - m) / w1
        t = int(np.argmax(np.nan_to_num(w0 * w1 * (mu1 - mu0) ** 2)))
    # A blank side would otherwise be split down the middle of its paper noise.
    return t if mu1[t] - mu0[t] >= MIN_CONTRAST else DARK


def to_lineart(path: str) -> str:
    """Write the side as a 1-bit PNG beside it and return that path."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        if im.mode == "1":
            return path
        kw = {"dpi": im.info["dpi"]} if "dpi" in im.info else {}
        g = im.convert("L")
    t = otsu(np.asarray(g))
    out = os.path.splitext(path)[0] + ".png"
    g.point([0] * (t + 1) + [255] * (255 - t), "1").save(out, **kw)
    return out


def to_gray(path: str) -> str:
    """Rewrite the side in place as an 8-bit gray JPEG."""
    from PIL import Image

    with Image.open(path) as im:
        # The scan's own luma table: no larger, and barely a generation lost.
        q = getattr(im, "quantization", None)
        kw = {"qtables": [q[0]]} if q else {"quality": 90}
        if "dpi" in im.info:
            kw["dpi"] = im.info["dpi"]
        g = im.convert("L")
    g.save(path, "JPEG", **kw)
    return path


def reduce_pages(
    paths: Sequence[str], mode: str, log: Callable[[str], None] = print
) -> list[str]:
    """Apply a lineart or auto `mode` to the sides; any other mode is a no-op."""
    mode = mode.strip().lower()
    if mode not in ("lineart", "auto"):
        return list(paths)
    if not available():
        log(f"  {mode} needs Pillow and numpy; keeping the sides as scanned")
        return list(paths)

    out = []
    for p in paths:
        try:
            if mode == "lineart":
                out.append(to_lineart(p))
                continue
            pct = colour_percent(p)
            keep = pct > PERCENT
            out.append(p if keep else to_gray(p))
            log(f"  {'colour' if keep else 'gray  '} {pct:7.3f}% coloured  {p}")
        except Exception as exc:  # noqa: BLE001 -- keep what the scanner sent
            log(f"  {p}: {exc} -- keeping it as scanned")
            out.append(p)
    if mode == "lineart":
        n = sum(a != b for a, b in zip(paths, out))
        log(f"  lineart: {n} of {len(out)} side(s) converted to black and white")
    return out
