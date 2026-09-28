# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""Enrol this machine in the scanner's host list.

The scanner keeps a list of hosts and shows their names on the panel. Until a
host is in that list it cannot register, and without a registration the panel's
Scan button stays dead.

Four things have to be right, and the failure for three of them is the same
unhelpful -2:

  * The document must carry conn_user AND profiles AND version. conn_user alone
    is accepted with status 0 and silently discarded.
  * The list is sent WHOLE. It is a replace, not an append, so the existing
    hosts have to be read first and passed back or they are dropped.
  * The write must sit inside a registration. Reads work unregistered, so this
    is easy to miss.
  * That registration must set intent 5 (see protocol.INTENT_CLAIM). With
    intent 0 the write is refused however correct the document is -- writing
    the list back byte-for-byte UNMODIFIED is refused too, so the -2 says
    nothing about the content.

Registration is refused -7 for any host_id the scanner has not enrolled, so a
machine that is not yet known cannot introduce itself directly. It registers as
a host the scanner does accept -- picked automatically from the existing list --
and adds itself. On a scanner with an empty list, its own id is accepted
straight away.

What CANNOT be set this way: `select` (which host the panel is pointed at) and
`profiles`. Those writes return 0 and are discarded -- they are scanner-managed.
Switch hosts by tapping the name on the panel itself.
"""

from __future__ import annotations

from typing import Callable

from .protocol import INTENT_CLAIM, explain_status
from .session import Session


def build_document(current: dict, host_id: str, name: str) -> dict:
    """Add this host to the existing list, preserving everything else."""
    conn = current.get("conn_user") or {}
    users = [u for u in conn.get("users", []) if u.get("host_id") != host_id]
    users.append({
        "app_ver": 1,
        "color_id": 4,
        "host_id": host_id,
        "icon_id": 1,
        "name": name,
        "prof_stat": 1,
        "scloud_user": "",
    })  # fmt: skip
    sel = conn.get("select") or {}
    if not sel.get("id"):
        sel = {"id": host_id, "kind": 1}
    return {
        "conn_user": {"select": sel, "users": users},
        # Passed back untouched. Dropping them would delete the profiles the
        # panel shows.
        "profiles": current.get("profiles", []),
        "version": current.get("version", 3),
    }


def enrol(
    host: str,
    host_id: str,
    name: str,
    dry_run: bool = False,
    via: str = "",
    log: Callable[[str], None] = print,
) -> bool:
    """Add this host to the scanner's list. Returns True if it stuck."""
    s = Session(host, host_id)
    current = s.host_list()
    users = current.get("conn_user", {}).get("users", [])
    log(f"scanner currently knows {len(users)} host(s):")
    for u in users:
        log(f"    {u.get('host_id')}  {u.get('name')!r}")

    doc = build_document(current, host_id, name)
    log("host list becomes:")
    for u in doc["conn_user"]["users"]:
        log(
            f"    {u['host_id']}  {u['name']!r}"
            + ("  <- us" if u["host_id"] == host_id else "")
        )
    if dry_run:
        log("dry run: nothing sent")
        return True

    # Register as somebody the scanner accepts. Our own id works on a scanner
    # with an empty list; otherwise borrow one that is already enrolled.
    reg_id = via or host_id
    writer = Session(host, reg_id)
    ok, why = writer.register(intent=INTENT_CLAIM, wait=30)
    if not ok and not via:
        borrowed = (current.get("conn_user", {}).get("select") or {}).get("id")
        if not borrowed and users:
            borrowed = users[0].get("host_id")
        if borrowed:
            log(f"  not enrolled yet ({why}); registering as {borrowed} instead")
            writer = Session(host, borrowed)
            ok, why = writer.register(intent=INTENT_CLAIM, wait=30)
    if not ok:
        log(f"  could not register to write the list: {why}{explain_status(why)}")
        return False

    try:
        st = writer.write_host_list(doc)
    finally:
        writer.unregister()
    log(f"  write returned {st}")

    after = Session(host, host_id).host_list()
    names = [u.get("name") for u in after.get("conn_user", {}).get("users", [])]
    if name in names:
        log(f"enrolled. host list is now: {names}")
        return True
    log(f"NOT enrolled -- the write was accepted but did not stick. List: {names}")
    return False
