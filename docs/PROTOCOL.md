# The ScanSnap iX1500 panel and network protocol

Reverse-engineered from packet captures of ScanSnap Home talking to an iX1500,
for interoperability. No vendor code was read or used. Everything here was
established by observing traffic and then confirming it against the hardware —
where a claim is inferred rather than tested, it says so.

Written for someone implementing this elsewhere: a SANE backend, a print/scan
server, another language. It is deliberately explicit about the things that
cost days to find, because none of them are guessable and several look like
padding.

Device: ScanSnap iX1500, USB `04c5:159f`. The iX1600 (`04c5:1632`) speaks the
same protocol; where it differs, this document says so. The iX1400 and related
models share the platform — untested.

- [The problem this solves](#the-problem-this-solves)
- [Transport](#transport)
- [Framing](#framing)
- [Discovery](#discovery)
- [Registration](#registration)
- [The `SETUP PROF INFO` document channel](#the-setup-prof-info-document-channel)
- [Enrolment](#enrolment)
- [Detecting a button press](#detecting-a-button-press)
- [Scanning](#scanning)
- [Making the panel dim](#making-the-panel-dim)
- [Status codes](#status-codes)
- [The two transports are exclusive](#the-two-transports-are-exclusive)
- [The USB transport](#the-usb-transport)
- [Notes for a SANE backend](#notes-for-a-sane-backend)

## The problem this solves

The iX1500 has no scan button in the ordinary sense. Its touch panel is a
client of whatever host currently owns it, and it is only enabled while a host
holds a **registration**. With no registered host the panel greys its Scan
button out within about a minute. That is why plugging the scanner into Linux
and running `scanimage` gives you a working scanner with a dead panel.

Worse, the two are mutually exclusive over USB: while a network registration is
active, USB scanning fails with `SANE_STATUS_IO_ERROR`. The registration that
keeps the panel alive is the same thing that locks SANE out.

So supporting this device properly means speaking the network protocol, not
just SCSI over USB.

## Transport

| port | proto | direction | purpose |
|---|---|---|---|
| 52217 | UDP | host → scanner | discovery probe |
| 53218 | TCP | host → scanner | control: SCSI passthrough |
| 53219 | TCP | host → scanner | one request per connection: registration, documents |
| 53220 | UDP | scanner → broadcast | "I have just booted" |
| 55265 | UDP | scanner → host | "the Scan button was pressed" |

Two behaviours to get right before anything works:

* **The scanner greets first.** On accepting a TCP connection it sends an
  unsolicited 16-byte frame with opcode 0. Sending your own frame before
  reading it gets the connection reset.
* **One request per connection on 53219.** Every request in the capture is its
  own TCP stream, opened and closed around a single exchange.

If the UDP ports are not bound, the kernel answers each notice with an ICMP
port-unreachable and the presses are simply lost. Bind them, or open them in
the firewall, before concluding the scanner does not send notices.

## Framing

```
 0  u32 BE   total length, including this field
 4  4 bytes  "ssNR"
 8  u32 BE   opcode
12  u32 BE   flags (0 in everything observed)
16  ...      payload
```

Discovery datagrams also use the magic `VENS`; ScanSnap Home sends a `VENS`
probe immediately followed by an `ssNR` one.

Opcodes seen from the host:

| opcode | meaning |
|---|---|
| `0x01` | SCSI passthrough (on 53218) |
| `0x11` | register |
| `0x12` | unregister |
| `0x13` | name and serial |
| `0x41` | read or write a document (host list, session, profile selection) |

A reply's opcode field carries the **status**, as a signed 32-bit value.

## Discovery

Host → scanner, UDP 52217, 32 bytes:

```
 0  4 bytes  "VENS" or "ssNR"
 8  4 bytes  host IPv4
12  6 bytes  host MAC
20  4 bytes  00 00 00 ff
24  u32 LE   0x1000 for VENS, 0x01 for ssNR
```

The scanner replies from 52217 to the source port with a 40-byte frame
containing its own address.

**It answers unicast probes but ignores broadcasts** — verified on hardware:
a unicast probe to the scanner is answered every time, while the same probe to
the subnet broadcast and to 255.255.255.255 gets nothing. Discovery by
broadcast alone therefore never finds it. Sweep the local subnet with unicast
probes, or let the user configure the address.

## Registration

Registration is what keeps the panel alive. Send `op 0x11` on 53219 every 15
seconds or so; the panel dies about a minute after you stop.

The payload is 368 bytes:

```
0x00  6 bytes  host MAC, then padding
0x10  4 bytes  01 00 1e 00              constant in every capture
0x14  u32 BE   INTENT                   see below -- this is not padding
0x1c  4 bytes  host IPv4
0x20  u32 BE   port for button notices  (55265)
0x54  u16 BE   year
0x56  u8       month, day, hour, minute, second
0x5c  8 bytes  host id
0x64  4 bytes  ff ff 73 60              constant
```

### Registration intent

**Offset 0x14 decides both who may register and who may write the host list.**
It reads as padding between the `01 00 1e 00` constant and the IP, and is zero
in most captures, which is exactly how it gets missed.

| intent | effect |
|---|---|
| 0 | accepted **only** for the one host_id the scanner currently treats as its own. Every other id is refused `-7`. A host-list write is refused `-2`. |
| 5 | accepted for **any** host_id, and permits the host-list write. |

ScanSnap Home sends 5 for exactly one registration during setup, and 0 for
every other registration for the rest of its life.

Intent 5 also means *a host is connecting*: the panel shows "Processing…" for
as long as claims keep arriving. **Do not send 5 on every cycle** — the panel
never returns to the Scan button. Send 0, fall back to 5 once when refused, and
go back to 0.

Nothing a host can write changes which id intent 0 accepts. Adding yourself to
the host list, pointing `select` at yourself, and writing the binary host_id
binding (subject 0x0c) are each accepted with status 0 and each leave intent 0
still refusing you.

### Unregister

`op 0x12`, 16-byte payload: the MAC, then zeros, with `0x01` at offset 11.
Leaving a registration open keeps the scanner claimed and locks other hosts out
until it times out.

## The `SETUP PROF INFO` document channel

Structured documents — the host list, the session, profile selection — travel
over one mechanism, on both transports. A 16-byte sub-header precedes a JSON
body:

```
0  u8     subject
1  u8     direction: 0x10 write, 0x00 read
12 u32 LE length
```

| subject | contents |
|---|---|
| `0x01` | `conn_user`: the host list, the profiles, and which host is selected |
| `0x02` | the session: which user the panel is acting for |
| `0x04` | a session document with a different user id (role not established) |
| `0x06` | profile selection: `{"version":1,"prof_id":"..."}` |
| `0x07` | read-only; contents not decoded |
| `0x0c` | binary host_id ↔ name binding (see below) |

### The length field means different things on each transport

| transport | offset 12 counts |
|---|---|
| USB (`SEND DIAGNOSTIC`) | the JSON only |
| network (`op 0x41`) | the JSON **plus the 16-byte sub-header** |

Confirmed on every write in the capture, always exactly +16. Declaring it 16
short truncates the JSON at the scanner, so it fails to parse and the write is
refused `-2`. Small documents survive the truncation, so this hides until a
6 KB host list makes it fatal.

On the network the whole thing is wrapped in a further 20-byte header: the host
MAC at offset 0, and the length of everything after it as `u32 BE` at offset
16.

### Subject 0x0c

A binary mirror of the host list, 180 bytes:

```
0   u32 LE  number of entries (1 observed)
4   8 bytes host id
12  u8      app version
13  u8      icon id
14  u8      colour id
15  u8      zero
16  ...     name, NUL-terminated, padded to the end
```

Writing this is accepted, and appears to change nothing on its own.

## Enrolment

The scanner keeps a list of hosts and shows their names on the panel. Four
things must all be right, and three of them fail with the same `-2`:

1. The document must carry `conn_user` **and** `profiles` **and** `version`.
   `conn_user` alone is accepted with status 0 and silently discarded.
2. The list is a **replace, not an append**. Read it, add yourself, send the
   whole thing back, or the other hosts are dropped.
3. The write must sit **inside a registration**. Reads work unregistered.
4. That registration must use **intent 5**.

Because the `-2` is identical for the last three, and a byte-identical echo of
the document is also refused, the status tells you nothing about the content.

```json
{"conn_user": {"select": {"id": "<host_id>", "kind": 1},
               "users": [{"app_ver": 1, "color_id": 1, "host_id": "<host_id>",
                          "icon_id": 8, "name": "<name>", "prof_stat": 1,
                          "scloud_user": ""}]},
 "profiles": [...],
 "version": 3}
```

`host_id` is 16 hex characters (8 bytes) **computed by the host**, not issued by
the scanner: a factory reset followed by re-pairing reproduced the identical
host_id and the same profile ids. Any implementation can mint its own; deriving
it from something stable like `/etc/machine-id` matches the behaviour.

There is no pairing code and no confirmation prompt. Whoever can reach port
53219 can write the host list.

Registration is refused `-7` for a host_id the scanner has not enrolled, so a
new machine cannot introduce itself directly. Register as a host that is
already in the list — any of them — and add yourself. On a scanner with an
empty list, your own id is accepted immediately.

### What a host cannot set

`select` (which host the panel is pointed at) and `profiles` are
**scanner-managed**. Writing them returns status 0 and changes nothing; the
read-back is unchanged. Switching hosts is done by tapping the name on the
panel. Any implementation should tell the user to do that rather than trying to
do it for them.

### The user id

Profiles carry a 32-hex-character `user_id`, and the session document (subject
0x02) must quote it as `current_user_id`. It is **per-scanner** — read it from
`profiles[*].user_id` rather than hard-coding one.

## Detecting a button press

Two independent mechanisms; use both, because the notice can be dropped and the
status bit is set only for about half a second.

**UDP notice** to port 55265. Only **opcode 0x01** is a press. Each notice
arrives three times, and byte 0 of the payload is a counter that increments per
press, not an event type — so deduplicate on the port and a short time window,
not on the payload.

Opcodes `0x10` and `0x11` also arrive on that port and are **not** presses. In
a full log every `0x01` is followed by a successful scan, while every `0x10` is
followed by a failed scan and a boot notice about 20 seconds later. Treating
them as presses makes the scanner start jobs by itself.

**`GET_HW_STATUS`**, CDB `c2 00 00 00 00 00 00 00 30 00`, tunnelled as SCSI
over the network:

```
byte 3 & 0x80   hopper is EMPTY
byte 4 & 0x01   Scan button pressed
byte 4 & 0x80   asleep
```

Once a network host is registered the scanner reports presses here and stops
setting `scan_sw` on the USB interface, so a USB poller sees nothing while the
panel is plainly reacting.

## Scanning

Register, open the session (subject 0x02), select a profile (subject 0x06),
then open 53218 and run SCSI. The scan connection must carry the setup sequence
and **nothing in front of it** — issuing anything else first makes `d4` fail
with `-1`.

Vendor commands, once per batch:

```
d5 (01)   [d5 00 00 01 08 08]  + 8 zero bytes
d8        begin
e9        config
d4        parameters, 80-byte block
d5 (00)   [d5 00 00 00 08 08]  + 8 zero bytes
```

Then, **per sheet**:

```
e0                            start THIS sheet
28 ... side 0x00              read front
03                            REQUEST SENSE
28 ... side 0x80              read back
03                            REQUEST SENSE
```

and to finish the batch:

```
e0                            starts a sheet with no paper behind it
d6                            close
```

`e0` is **per sheet, not per batch**. One `e0` for the whole batch makes sheet 2
return 2 bytes with paper still in the hopper.

Both terminators must be sent even if the batch failed. Without them the panel
sits on "Scanning…" and eventually reports the connection was lost — and a
crashed handler is exactly when that happens.

The image comes back as **JPEG**, in many frames; strip `0x18` bytes from each
frame body and concatenate. A read is complete at the JPEG EOI (`ff d9`).
Waiting for a quiet period instead costs seconds per side, which is long enough
for the scanner to abandon the job.

### Ending a batch

The hopper-empty sense documented in sane-backends (key 0x3 / ASC 0x80 /
ASCQ 0x03) **never arrives on this transport** — the key stays 0 with EOM set
through the last sheet. Decide whether to continue by checking `GET_HW_STATUS`
for paper **before** reading the next sheet, on a separate connection.

Reading speculatively and stopping when the data comes back empty produces the
right images but leaves the job open, and the panel then hangs on "Scanning…"
even though `e0` and `d6` both return 0.

Sense is still worth reading, to catch real faults:

| key, ASC, ASCQ | meaning |
|---|---|
| 03 80 01 | paper jam |
| 03 80 02 | cover open |
| 03 80 04 | unusual paper |
| 03 80 07 | double feed |
| 03 80 08 | no paper picked |

### Resolution

The `d4` parameter block is 80 bytes and only partly decoded. The bytes that
look like padding hold the resolution and page geometry: `012c 012c` is 300 dpi
in each axis and `28d0 x 45a4` is the page in 1/1200 inch. Changing them
blindly makes `d4` fail, so the network path is currently fixed at 300 dpi
colour. Decoding this block properly is the most useful outstanding piece of
work.

### The network transport allow-lists SCSI

`op 0x01` tunnels a CDB, but only a few are accepted: `INQUIRY`,
`REQUEST SENSE`, `GET_HW_STATUS`, and the vendor set `d4 d5 d6 d8 e0 e9`. The
vendor commands only become available **after** a successful registration.

Anything outside that set is answered with **status 0 and an empty payload**
rather than an error, which is easy to mistake for success. `MODE SENSE` (0x1A)
behaves this way, so mode pages cannot be read back over the network at all —
and by implication a `MODE SELECT` that appears to succeed there may have done
nothing.

## Making the panel dim

Two things decide it, and they are independent:

1. **The scanner's sleep timer**, `MODE SELECT` mode page 0x34, in minutes. It
   is what actually turns the backlight off.
2. **Registration counts as activity and resets it.** A host that keeps
   registering keeps the panel lit for ever, whatever the timer says. Measured:
   dark at 84.5s, a registration 30s later, relit 2s after that.

So to let the panel go dark, stop registering and wait out the timer. To keep
it lit, keep registering. Polling does **not** count as activity and does not
prevent the dim — measured at 776s with nothing at all talking to the scanner,
against 777s for an otherwise identical polled run.

### The sleep timer

```
15 10 00 00 0c 00                          MODE SELECT(6)
00 00 00 00 34 06 NN 00 00 00 00 00        4-byte header, then the page
```

Read it back with `MODE SENSE(6)`, `1a 00 34 00 14 00`. **The page begins at
offset 12 of the reply, not offset 4** — there is an 8-byte block descriptor in
between, and reading the value from offset 6 gets a byte of that descriptor,
which is always zero. That misreading cost two wrong conclusions here:

```
13 00 00 08 | 00 00 00 00 00 00 00 01 | 34 06 0f 00 00 00 00 00
header      | block descriptor        | page code, length, VALUE
```

Values are clamped, silently, and the write always reports success:

| written | stored |
|---|---|
| 0 | 0 — accepted; presumably "never", not verified |
| 1 | **2** — 2 minutes is the minimum |
| 2, 3, 15, 30, 60 | as written |
| 255 | **224** — the maximum |

Always read the value back rather than trusting the status.

**Page 0x34 works over USB only.** Over the network the write is accepted with
status 0 and has no effect: a timer set to 30 that way left the panel dimming
on its old value. `MODE SENSE` is not carried on that transport either — it too
returns status 0 with an empty payload — so there is no way to notice from the
network side. See the allowlist note under Scanning.

### Verified behaviour

| timer | conditions | dark after |
|---|---|---|
| default (~15) | polled, nothing registering | 777s |
| default (~15) | nothing touching the scanner at all | 776s |
| 30, set over the network | nothing touching the scanner | 883s — the write was ignored |
| 2, verified over USB | nothing touching the scanner | ~2 min |

The delay is the timer, not a property of the device. An earlier version of
this document described it as an erratic 93–771s; that was registration
resetting the clock at different points, plus the timer sitting at values
nobody had read correctly.

**Waking.** Touching the panel wakes it and clears the sleep bit
(`GET_HW_STATUS` byte 4 & 0x80), so a poller sees it within one interval. On
the network a registration relights it immediately. Over USB **nothing the host
can send wakes it** — arming and polling both leave the sleep bit set — so
after the timer expires the Scan button needs a physical touch.

## The two transports are exclusive

A USB cable does not merely take precedence — it makes the network path
unavailable. With a cable attached, `op 0x11` registration is refused `-4`
indefinitely, so the panel cannot be kept alive over the network at all.

Observed directly: a daemon that had been registering successfully for hours
began refusing at the first attempt after the cable was plugged in, and every
attempt for the following twenty minutes was refused. `GET_HW_STATUS` byte 0x10
still read 0x80 ("free") throughout, so that byte is not a reliable indicator of
this state. Closing the USB-side session did not release it; nor did leaving USB
completely untouched for a minute.

This is the same precedence ScanSnap Home shows: with a cable connected it uses
USB even when the panel was configured for Wi-Fi, and a "Wi-Fi" capture made
that way silently records a USB session.

The iX1600 does not do this: with a cable attached it goes on accepting
network registration, and a daemon registered over Wi-Fi stays usable.

The practical consequence for an implementation is that transport is not a
preference to be tuned, it is decided by whether a cable is plugged in. Detect
the cable (sysfs `04c5:159f`, or `04c5:1632` for an iX1600) and use USB when it is present; a `-4` that never
clears usually means a cable, not a busy scanner.

## The USB transport

Standard Fujitsu SCSI-over-USB: a 31-byte command envelope beginning `0x43`
with the CDB at offset 0x13, a 13-byte status envelope beginning `0x53`, bulk
endpoints `0x02` OUT and `0x81` IN.

Scanning over USB uses the ordinary Fujitsu sequence — `MODE SELECT` pages,
`SET WINDOW`, gamma tables via `WRITE`, `OBJECT POSITION` to feed, `SCAN`, then
`READ` — and works. Unlike the network path it needs no registration and no
host list at all, so it works on a scanner out of the box, and resolution and
colour mode are adjustable.

A warning from experience: **replay the captured setup rather than
reconstructing it.** Every constant that got hand-typed from a capture here was
truncated, and the failures did not look like truncation. The worst was `SET
WINDOW`: the payload is 136 bytes, an 8-byte header and **two** 64-byte window
descriptors, one per side. A version with a single descriptor is accepted, and
then duplex stalls in a way that reads as a transport bug. Patch fields into
the captured bytes; do not retype them.

Other things worth knowing:

* `SET WINDOW` carries **two** 64-byte window descriptors, one per side.
  Sending one descriptor makes duplex stall in a way that looks like a
  transport bug.
* `SCAN` needs a data-out phase carrying the window ids (`00 80`).
* Sense `ASCQ 0x13` during a read means "still scanning", not an error.
* Inserting a `REQUEST SENSE` between the feed and `SCAN` causes a
  command-sequence error.
* Image data is **inverted**, and the tail is padded with `0x55`. Width is
  `10448 * resolution / 1200` pixels.
* The vendor commands `d4`, `d5` and `e0` are **network only**; over USB they
  return Overflow or time out.

The document channel over USB is `SEND DIAGNOSTIC` with a 16-byte
space-padded name `"SETUP PROF INFO "`, then a 16-byte operation header, then
the sub-header and JSON described above. It has no registration concept, so it
works whatever the host list holds. The five-step transaction is ops
`00, 01, 02, 03, 04`; op `03` is **not** acknowledged by an 8-byte read, and
issuing one consumes the head of the reply.

## Notes for a SANE backend

The existing `fujitsu` backend already scans this device over USB. What it
cannot do is keep the panel alive, because that needs the network protocol and
a host identity.

* **The panel and USB scanning are mutually exclusive.** Any design that keeps
  a registration for the panel must scan over the network too, or accept that
  `scanimage` will fail while the panel works.
* **`f1 04` vs `f1 09`.** The backend sends `f1 09` where the vendor sends
  `f1 04` to end a job. With `09` the panel stays on "Scanning…" after a
  SANE-driven scan. This is a small, self-contained fix worth making regardless
  of anything else here.
* The blank-page logic in `sanei_magic_isBlank2()` measures mean darkness,
  which does not survive real iX1500 output: the paper background alone is
  about 4.5% darkness, so a blank reverse side scores higher than a text page's
  median block. Counting the fraction of pixels that are actually ink separates
  them cleanly — a blank side measures exactly zero. Ignore a quarter inch at
  each edge first; ADF shadow accounts for all of the 0.24% a blank side would
  otherwise show.

Licensed GPL-2.0-or-later, deliberately, so this and the accompanying
implementation can be used in sane-backends without a licensing problem.
