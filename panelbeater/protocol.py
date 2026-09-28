# Copyright (C) 2026 Jenna Nelson
# SPDX-License-Identifier: GPL-2.0-or-later
"""The ssNR wire protocol: framing, discovery, registration.

Reimplemented from packet captures of ScanSnap Home talking to an iX1500. No
vendor code is used here. docs/PROTOCOL.md documents the format and how each
field was established.

    u32 BE total length (whole frame) | "ssNR" | u32 BE opcode | u32 BE flags | payload

    TCP 53218  control: SCSI passthrough (opcode 0x01)
    TCP 53219  per-request: registration, host list, profiles
    UDP 53220  scanner broadcasts "I have booted"
    UDP 52217  host broadcasts discovery
    UDP 55265  scanner tells the host the Scan button was pressed
"""

from __future__ import annotations

import fcntl
import socket
import struct
import time

MAGIC_SSNR = b"ssNR"
MAGIC_VENS = b"VENS"

PORT_DISCOVER = 52217
PORT_CONTROL = 53218
PORT_REQUEST = 53219
PORT_HOST_NOTIFY = 53220
PORT_BUTTON_NOTIFY = 55265

OP_SCSI = 0x01
OP_REGISTER = 0x11
OP_UNREGISTER = 0x12
OP_NAME = 0x13
OP_PROF_INFO = 0x41

# Only opcode 0x01 arriving on the button port is a Scan press. The daemon used
# to react to any notice on that port, which made it start a scan by itself
# after a finished job. Across a full log the split is clean:
#
#   op 0x01  ->  a scan follows and succeeds, every time
#   op 0x10  ->  the scan fails, and the scanner broadcasts a boot notice ~20s
#                later; seen three times
#   op 0x11  ->  the scan fails
#
# So 0x10 and 0x11 announce something else and cluster with the scanner going
# down, not with a person touching the panel.
OP_BUTTON_NOTICE = 0x01

# Offset 0x14 of the registration payload. 0 is refused (-7) for every host_id
# except the one the scanner already treats as its own, and makes a host-list
# write fail with -2; 5 is accepted for any host_id and permits the write.
# ScanSnap Home sends 5 for exactly one registration during setup and 0 for
# every other. See docs/PROTOCOL.md, "Registration intent".
INTENT_NORMAL = 0
INTENT_CLAIM = 5


def signed(v: int) -> int:
    """Status codes come back as u32 but are negative errors."""
    return v - (1 << 32) if v > 0x7FFFFFFF else v


STATUS_HINTS = {
    -4: (
        "the scanner is busy: a USB cable is attached, another computer holds "
        "it, or it is running a job of its own such as a ScanSnap Cloud "
        "profile. Unplug the cable, or wait for the job to finish (or cancel "
        "it on the panel) and try again"
    ),
}


def explain_status(status: int) -> str:
    """' -- why', for a status worth explaining, else ''."""
    hint = STATUS_HINTS.get(status)
    return f" -- {hint}" if hint else ""


def local_ip_and_mac(target: str = "8.8.8.8") -> tuple[str, bytes, str]:
    """(ip, mac, ifname) for the interface that would reach `target`.

    No packet is sent -- connect() on a UDP socket only sets the route. The
    target just has to be off-link; it is never contacted.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        ip = s.getsockname()[0]
    finally:
        s.close()

    ifname = ""
    try:
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if f[1] == "00000000":
                    ifname = f[0]
                    break
    except OSError:
        pass

    mac = b"\x00" * 6
    if ifname:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            info = fcntl.ioctl(
                s.fileno(), 0x8927, struct.pack("256s", ifname[:15].encode())
            )
            mac = info[18:24]
        except OSError:
            pass
        finally:
            s.close()
    return ip, mac, ifname


def frame(opcode: int, payload: bytes = b"", flags: int = 0) -> bytes:
    body = MAGIC_SSNR + struct.pack(">II", opcode, flags) + payload
    return struct.pack(">I", len(body) + 4) + body


def parse_frame(buf: bytes) -> tuple[int, int, bytes] | None:
    if len(buf) < 16 or buf[4:8] != MAGIC_SSNR:
        return None
    total = struct.unpack(">I", buf[0:4])[0]
    opcode, flags = struct.unpack(">II", buf[8:16])
    return opcode, flags, buf[16:total]


def discovery_packet(magic: bytes, ip: str, mac: bytes, tail: int) -> bytes:
    """32-byte host discovery datagram, as ScanSnap Home sends it."""
    p = bytearray(32)
    p[0:4] = magic
    p[8:12] = socket.inet_aton(ip)
    p[12:18] = mac
    p[20:24] = b"\x00\x00\x00\xff"
    p[24:28] = struct.pack("<I", tail)
    return bytes(p)


def registration_payload(
    host_ip: str,
    mac: bytes,
    host_id: bytes,
    when: time.struct_time | None = None,
    trigger_port: int = PORT_BUTTON_NOTIFY,
    intent: int = INTENT_NORMAL,
) -> bytes:
    """The 368-byte opcode 0x11 payload.

        0x00  host MAC, then padding
        0x10  01 00 1e 00                   constant in every capture
        0x14  u32 BE intent                 see INTENT_CLAIM
        0x1c  host IPv4
        0x20  u32 BE port the scanner should send button notices to
        0x54  u16 BE year, then month, day, hour, minute, second
        0x5c  8-byte host id
        0x64  ff ff 73 60                   constant

    Offset 0x14 was originally read as part of the zero padding between the
    constant and the IP, because it is 0 in most captures. It is not padding.
    """
    when = when or time.localtime()
    p = bytearray(368)
    p[0:6] = mac
    p[0x10:0x14] = bytes([0x01, 0x00, 0x1E, 0x00])
    p[0x14:0x18] = struct.pack(">I", intent)
    p[0x1C:0x20] = socket.inet_aton(host_ip)
    p[0x20:0x24] = struct.pack(">I", trigger_port)
    p[0x54:0x56] = struct.pack(">H", when.tm_year)
    p[0x56] = when.tm_mon
    p[0x57] = when.tm_mday
    p[0x58] = when.tm_hour
    p[0x59] = when.tm_min
    p[0x5A] = when.tm_sec
    p[0x5C:0x64] = host_id
    p[0x64:0x68] = bytes([0xFF, 0xFF, 0x73, 0x60])
    return bytes(p)


def tcp_request(
    host: str, port: int, opcode: int, payload: bytes = b"", timeout: float = 5.0
) -> bytes:
    """One request on a fresh connection.

    The SCANNER greets first: on accept it sends an unsolicited 16-byte frame
    with opcode 0. Sending ours at it instead gets the connection reset, so its
    greeting has to be read before asking anything.
    """
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.recv(4096)
        s.sendall(frame(opcode, payload))
        buf = b""
        try:
            while True:
                d = s.recv(8192)
                if not d:
                    break
                buf += d
                if len(buf) >= 16 and len(buf) >= struct.unpack(">I", buf[0:4])[0]:
                    break
        except socket.timeout:
            pass
    return buf


def hw_status(host: str, mac: bytes, length: int = 0x30) -> bytes:
    """GET_HW_STATUS through the network transport.

    Once a network host is registered the scanner reports the Scan button here
    and stops setting scan_sw on the USB interface, so a USB poller sees nothing
    while the panel is plainly reacting.

    Useful bits of the returned block:
        byte 3 & 0x80   hopper is EMPTY
        byte 4 & 0x01   Scan button is pressed
        byte 4 & 0x80   scanner is asleep
    """
    cdb = bytes([0xC2, 0, 0, 0, 0, 0, 0, length >> 8, length & 0xFF, 0])
    p = bytearray(48)
    p[0:6] = mac
    p[16:20] = struct.pack(">I", len(cdb))
    p[20:24] = struct.pack(">I", length)
    p[32 : 32 + len(cdb)] = cdb
    buf = tcp_request(host, PORT_CONTROL, OP_SCSI, bytes(p), timeout=4)
    parsed = parse_frame(buf)
    if not parsed:
        raise OSError("malformed GET_HW_STATUS reply")
    return parsed[2][0x18 : 0x18 + length]


def local_subnet(ifname: str, ip: str, max_hosts: int = 1024) -> list[str]:
    """Every address on our subnet, from the kernel routing table.

    Falls back to the /24 around our own address if the real prefix is missing
    or too large to sweep -- nobody wants a /16 probed one host at a time.
    """
    import ipaddress

    # The values in /proc/net/route are little-endian on x86, so they have to
    # go back through inet_ntoa rather than being used as integers. Doing the
    # netmask arithmetic on them directly yields a complement of 0xff000000 for
    # a /24 and a sweep of four billion addresses.
    net = None
    try:
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if f[0] != ifname:
                    continue
                dest = socket.inet_ntoa(struct.pack("<L", int(f[1], 16)))
                mask = socket.inet_ntoa(struct.pack("<L", int(f[7], 16)))
                if mask == "0.0.0.0":
                    continue
                cand = ipaddress.ip_network(f"{dest}/{mask}", strict=False)
                if ipaddress.ip_address(ip) in cand:
                    net = cand
                    break
    except (OSError, ValueError):
        net = None
    if net is None or net.num_addresses > max_hosts:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return [str(h) for h in net.hosts() if str(h) != ip]


def discover(timeout: float = 4.0, sweep: bool = True) -> list[str]:
    """Find scanners.

    Broadcast first, because it is one packet. Not every scanner answers a
    broadcast though -- an iX1500 that replies happily to a unicast probe
    ignores both the subnet and the global broadcast -- so unless told
    otherwise this falls back to probing each address on the local subnet.
    That is a few hundred small datagrams on a home network and takes about a
    second, which beats a discovery that silently never works.
    """
    ip, mac, ifname = local_ip_and_mac()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    s.bind(("", 0))

    def probe(dest: str) -> None:
        # ScanSnap Home sends both magics, VENS first.
        for magic, tail in ((MAGIC_VENS, 0x1000), (MAGIC_SSNR, 0x01)):
            try:
                s.sendto(discovery_packet(magic, ip, mac, tail), (dest, PORT_DISCOVER))
            except OSError:
                pass

    found: list[str] = []

    def collect(until: float) -> None:
        while time.monotonic() < until:
            try:
                _, addr = s.recvfrom(2048)
            except socket.timeout:
                return
            except OSError:
                return
            if addr[0] not in found:
                found.append(addr[0])

    for dest in ("255.255.255.255", "<broadcast>"):
        probe(dest)
    collect(time.monotonic() + min(1.5, timeout))

    if not found and sweep and ifname:
        for addr in local_subnet(ifname, ip):
            probe(addr)
        collect(time.monotonic() + timeout)

    s.close()
    return found
