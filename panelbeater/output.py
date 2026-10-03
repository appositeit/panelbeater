# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Turning scanned sides into a filed document.

    scan -> JPEGs in a work dir
         -> drop blank sides
         -> assemble a PDF in the STAGING dir
         -> run the rename hook, if configured
         -> move into the output dir

The staging step exists because output_dir is very often watched by something
that ingests documents -- paperless-ngx, Nextcloud, a Syncthing share. A watcher
can take the file within seconds, and if the rename hook is still running when
that happens it renames a file that is no longer there. Naming happens in
staging, where nothing is watching, and the document is handed over only once it
is finished.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from . import blank


def _which(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def assemble_pdf(
    pages: Sequence[str], out: Path, log: Callable[[str], None] = print
) -> bool:
    """Combine JPEGs into one PDF, using whatever the system has.

    Tried in order of how well each preserves the original JPEG data:

      img2pdf   embeds the JPEGs losslessly, no re-encode  (best, pure Python)
      Pillow    re-encodes, so slightly lossy but universal
      convert   ImageMagick; may be blocked by a restrictive policy.xml

    Returns False if none are available, in which case the caller keeps the
    JPEGs -- a scan is never discarded just because no PDF tool is installed.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import img2pdf

        with open(out, "wb") as fh:
            fh.write(img2pdf.convert([str(p) for p in pages]))
        return True
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"  img2pdf failed ({exc}); trying another method")

    try:
        from PIL import Image

        ims = [Image.open(p).convert("RGB") for p in pages]
        ims[0].save(out, "PDF", save_all=True, append_images=ims[1:])
        for im in ims:
            im.close()
        return True
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log(f"  Pillow PDF failed ({exc}); trying another method")

    cmd = _which("img2pdf", "convert", "magick")
    if cmd:
        try:
            args = (
                [cmd, "convert", *map(str, pages), str(out)]
                if cmd.endswith("magick")
                else [cmd, *map(str, pages), str(out)]
            )
            if cmd.endswith("img2pdf"):
                args = [cmd, "-o", str(out), *map(str, pages)]
            subprocess.run(args, check=True, capture_output=True, timeout=300)
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"  {cmd} failed: {exc}")

    log("  no PDF tool available (install img2pdf, Pillow or ImageMagick)")
    return False


def ocr_in_place(
    path: Path, languages: str = "", log: Callable[[str], None] = print
) -> None:
    """Add a text layer with ocrmypdf, if it is installed.

    Entirely optional. A failure leaves the original untouched -- OCR is a
    convenience, never a step the document depends on.
    """
    cmd = _which("ocrmypdf")
    if not cmd:
        return
    tmp = path.with_suffix(".ocr.pdf")
    lang = ["-l", languages] if languages else []
    try:
        subprocess.run(
            [cmd, "--quiet", "--rotate-pages", "--deskew", "--skip-text", *lang,
             str(path), str(tmp)],
            check=True, capture_output=True, timeout=900,
        )  # fmt: skip
        tmp.replace(path)
        log("  OCR layer added")
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"  OCR skipped: {str(exc)[:120]}")
        tmp.unlink(missing_ok=True)


def run_hook(
    hook: str,
    path: Path,
    timeout: float = 300.0,
    log: Callable[[str], None] = print,
    env: dict[str, str] | None = None,
) -> Path:
    """Give the document to the hook and find out where it ended up.

    Contract (see hooks/README.md):
      * argv[1] is the absolute path of the PDF, sitting in the staging dir.
      * The hook may rename or move it, but must leave it inside that dir.
      * If it prints a path on stdout, that is taken as the new location.
      * A non-zero exit, a timeout, or a path that does not exist is ignored
        and the original is used.
      * Every configuration setting is passed in the environment as
        PANELBEATER_<KEY>, so a hook is configured from the same file as
        everything else. Putting its settings in the systemd unit instead makes
        `panelbeater scan` behave differently from the daemon, which is a
        confusing way to find out your hook was never configured.

    Any failure keeps the document under its timestamp name. Naming is a bonus
    applied afterwards, never something a scan depends on.
    """
    try:
        r = subprocess.run(
            [hook, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {}), "PANELBEATER_PDF": str(path)},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"  hook failed: {str(exc)[:160]}")
        return path if path.exists() else path

    for line in (r.stderr or "").strip().splitlines():
        log(f"  hook: {line}")
    if r.returncode != 0:
        log(f"  hook exited {r.returncode}; keeping {path.name}")
        return path

    said = (r.stdout or "").strip().splitlines()
    if said:
        cand = Path(said[-1].strip()).expanduser()
        # Only believe a path inside the staging directory. Whatever the hook
        # prints is used as the document we then file, so without this check a
        # buggy hook that echoes something else -- or a malicious one -- makes
        # us pick up an unrelated file and treat it as the scan.
        try:
            inside = cand.resolve().parent == path.parent.resolve()
        except OSError:
            inside = False
        if cand.is_file() and inside:
            return cand
        if cand.is_file():
            log(f"  ignoring hook output {cand}: outside the staging directory")
        else:
            log(f"  hook named a path that does not exist: {cand}")

    # The hook may have renamed it without telling us. If the original is gone
    # and exactly one PDF is left in staging, that is it.
    if not path.exists():
        left = sorted(path.parent.glob("*.pdf"))
        if len(left) == 1:
            log(f"  hook renamed it to {left[0].name}")
            return left[0]
        log(f"  hook removed {path.name} and left no single replacement")
    return path


def deliver(path: Path, dest: Path, log: Callable[[str], None] = print) -> Path:
    """Move the finished document into dest, atomically where possible.

    A plain rename within one filesystem is atomic, so a folder-watcher can
    never see a half-written file. Across filesystems it cannot be, hence the
    dotfile: watchers skip hidden files, so the copy stays invisible until the
    final rename publishes it.
    """
    dest.mkdir(parents=True, exist_ok=True)
    final = unique_path(dest / path.name)
    try:
        path.rename(final)
    except OSError:
        part = final.with_name(f".{final.name}.part")
        shutil.copy2(path, part)
        part.rename(final)
        path.unlink(missing_ok=True)
    log(f"  filed -> {final}")
    return final


def unique_path(target: Path) -> Path:
    """Never overwrite an existing document."""
    if not target.exists():
        return target
    for n in range(2, 1000):
        cand = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not cand.exists():
            return cand
    return target.with_name(f"{target.stem} ({os.getpid()}){target.suffix}")


def finish(
    pages: Sequence[str],
    stamp: str,
    cfg,
    log: Callable[[str], None] = print,
) -> Path | None:
    """Blank removal, assembly, naming, filing. Returns the final path."""
    pages = list(pages)
    if not pages:
        return None

    if cfg.flag("blank_removal"):
        kept = blank.keep_pages(pages, cfg.num("blank_threshold", 0.5), log=log)
        if kept:
            log(f"  blank removal: keeping {len(kept)} of {len(pages)} side(s)")
            pages = kept

    staging = cfg.staging_dir
    staging.mkdir(parents=True, exist_ok=True)

    if not cfg.flag("pdf"):
        # No PDF wanted: file the JPEGs individually and stop.
        out = None
        for i, p in enumerate(pages, 1):
            out = deliver(Path(p), cfg.output_dir, log=log)
        return out

    doc = staging / f"scan-{stamp}.pdf"
    if not assemble_pdf(pages, doc, log=log):
        for p in pages:
            deliver(Path(p), cfg.output_dir, log=log)
        return None
    log(f"  assembled {len(pages)} side(s) -> {doc.name}")
    ocr_in_place(doc, cfg.get("ocr_languages").strip(), log=log)

    hook = cfg.get("hook").strip()
    if hook:
        # Everything in the config reaches the hook, including keys panelbeater
        # itself does not use -- that is how a hook gets its own settings.
        hook_env = {f"PANELBEATER_{k.upper()}": str(v) for k, v in cfg.values.items()}
        doc = run_hook(hook, doc, cfg.num("hook_timeout", 300.0), log=log, env=hook_env)
        # A hook owns the file while it runs, so one that deletes it can still
        # lose a scan -- but that must be reported, not raised into the daemon's
        # scan thread from inside deliver().
        if not doc.exists():
            log(f"  the hook removed {doc.name} and left nothing to file")
            return None

    if doc.parent.resolve() == staging.resolve():
        return deliver(doc, cfg.output_dir, log=log)
    return doc
