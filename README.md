# panelbeater

Press the Scan button on a ScanSnap iX1500 and have the page arrive on your
Linux machine. No vendor software, no Windows VM.

The iX1500's touch panel is not a button — it is a client of whatever computer
currently owns the scanner, and it only works while that computer keeps telling
the scanner it is there. Without vendor software the panel greys out and the
scanner becomes a plain USB device you have to drive from the keyboard. This
implements the protocol the panel actually speaks, so the button works again.

```
$ panelbeater enrol
$ systemctl --user enable --now panelbeater
```

Then press Scan. A searchable PDF appears in `~/Documents/Scans`.

Tested on an iX1500 (`04c5:159f`) over Wi-Fi, and on an iX1600 (`04c5:1632`)
over Wi-Fi and USB. The iX1400 shares the platform and will probably work —
reports welcome.

## What works

- The Scan button on the panel starts a scan on your machine, over **Wi-Fi or
  USB**
- Multi-sheet batches from the ADF, duplex; 300 dpi colour over the
  network, adjustable over USB
- Blank reverse sides dropped automatically
- Combined into one PDF, with a text layer if `ocrmypdf` is installed
- Filed into a directory you choose, with an optional rename hook
- Enrolment: your machine appears on the panel by name, alongside any others
- Optional idle dimming, so the panel does not glow at you all night

## What does not

- **Resolution is fixed at 300 dpi colour over the network.** The parameter
  block that sets it is only partly decoded; see `docs/PROTOCOL.md`. Over USB
  it is adjustable.
- **Wi-Fi has to be configured on the panel itself.** The credentials are typed
  on the scanner and never cross the wire, so no host tool can do it.
- **Which host the panel points at is chosen on the panel.** A host can add
  itself to the list but cannot select itself; those writes are accepted and
  ignored by the scanner.
- **A USB cable disables the network path.** Not a preference: with a cable
  attached the scanner refuses network registration outright, so plugging one
  in switches you to USB whether you meant to or not — and takes idle dimming
  with it, since that only exists on the network path. (iX1500. An iX1600
  keeps accepting network registration with the cable attached; `auto` still
  picks USB when the daemon starts with the cable in.)
- **Over USB the panel dims by itself after ~13 minutes and only a touch wakes
  it.** There is no registration to relight it, and nothing the host can send
  will. After an idle spell the Scan button needs a tap on the screen first.
- **USB and SANE cannot both have the scanner.** Only one process can claim the
  interface. See [Scanning over USB](#scanning-over-usb).

## Requirements

Python 3.9 or newer. **The scan path itself uses only the standard library**,
so it works on a fresh machine with nothing installed. Everything else is
optional and degrades to a clear message rather than a traceback:

| for | install | without it |
|---|---|---|
| one PDF instead of loose JPEGs | `img2pdf`, or Pillow | you get the JPEGs |
| dropping blank reverse sides | Pillow and numpy | every side is kept |
| a searchable text layer | `ocrmypdf` | the PDF has no text layer |
| scanning over USB | `pyusb`, Pillow, numpy | the network path still works |

That is true of the **network** path. Scanning over USB does need `pyusb` and
an image library, because the scanner sends raw data there rather than JPEG.

## Install

```sh
./install.sh              # into ~/.local, plus a systemd --user unit
```

It copies a Python package and writes a launcher — nothing is compiled and no
distro packaging is involved. `./install.sh --prefix /usr/local` as root
installs system-wide; `./install.sh --uninstall` removes it and leaves your
config and scans alone.

There is a `pyproject.toml` if you prefer `pip install .`.

## Set up

```sh
panelbeater config --write     # a config file to edit, at ~/.config/panelbeater/config
panelbeater discover           # find the scanner
panelbeater enrol              # add this machine to its host list
panelbeater status             # check what the scanner thinks
```

`enrol` puts your machine in the scanner's list under a name you choose. Then,
**on the scanner's panel, tap the host name at the top and choose your
machine** — that part cannot be done from the computer.

```sh
systemctl --user enable --now panelbeater
loginctl enable-linger $USER    # so it runs when you are not logged in
```

`panelbeater status` is the thing to run when something is wrong: it prints the
config it loaded, whether the scanner is reachable, whether there is paper, and
every host the scanner knows about with the selected one marked.

## Configuration

`~/.config/panelbeater/config`, or `/etc/panelbeater/config`, or `$PANELBEATER_CONFIG`.
Every key can also be set as `PANELBEATER_<KEY>` in the environment, which is how
to override things in the systemd unit without editing it.

```ini
[panelbeater]
transport = auto            # network, usb, or auto (USB if the cable is there)
scanner = auto              # or an IP; auto discovers, which takes a few seconds
name =                      # how this host appears on the panel; default hostname
output_dir = ~/Documents/Scans
staging_dir =               # default: a .staging dir inside output_dir
hook =                      # optional rename program; see hooks/README.md
pdf = yes
blank_removal = yes
blank_threshold = 0.5
max_sheets = 100
interval = 15               # seconds between registrations; this is the keep-alive
dim_after = 0               # minutes idle before we stop registering; 0 = never
dim_timer = 0               # scanner's own sleep timer, minutes; USB only
```

Setting `scanner` to a fixed IP is worth doing: the iX1500 answers unicast
discovery but ignores broadcasts, so `auto` has to sweep the subnet.

## Renaming scans

Scans are filed as `scan-YYYYmmdd-HHMMSS.pdf` by default. Point `hook` at a
program to do better. **No LLM is required** — a hook is any executable that
takes a path:

| hook | needs | result |
|---|---|---|
| `hooks/rename-by-date` | nothing | `2026-08-11 1423.pdf` |
| `hooks/rename-by-text` | `pdftotext` | first real line of the text layer |
| `hooks/rename-with-ollama` | `ollama` | `Origin Electricity Bill January 2025` |

See `hooks/README.md` for the contract. A hook that fails, hangs or returns
nonsense cannot lose a scan — the document is filed under its timestamp name
instead.

### If something else watches your scans folder

paperless-ngx, Nextcloud and Syncthing take a new file within seconds and
usually delete the original. That is why documents are assembled and named in a
**staging directory** first and only moved into `output_dir` when finished —
otherwise a hook that takes a minute renames a file that is no longer there.
Keep both on the same filesystem so the handover is an atomic rename and the
watcher never sees a partial file.

## Letting the panel go dark

A scanner panel that glows all night is a nightlight nobody asked for. Two
things decide whether it is lit, and you may need both:

```ini
[panelbeater]
dim_after = 10      # minutes idle before we stop registering; 0 = never
dim_timer = 0       # the scanner's own sleep timer, in minutes; USB only
```

**Registration keeps the panel lit.** While panelbeater is registering, the
scanner never sleeps — that is also what keeps the Scan button alive, so the
two cannot both be had. `dim_after` is how long to wait before giving up the
registration.

**The scanner's own timer then decides when the backlight goes off**, about 15
minutes out of the box. So with `dim_after = 10` the panel goes dark roughly 25
minutes after you last touch it. Touch it and it comes straight back: the
daemon sees the wake within a poll and registers again.

To make it darker sooner, shorten the scanner's timer with `dim_timer` — but
**that only works over USB.** On the network the write is accepted and silently
ignored. Plug the cable in once with `dim_timer = 2`, let panelbeater set it,
and the setting sticks in the scanner afterwards.

Values are clamped by the scanner: 2 minutes is the minimum, 224 the maximum,
and `dim_timer = 0` leaves whatever is there alone.

Over USB nothing registers, so the panel simply follows its own timer, and
**only a physical touch wakes it** — arming and polling both leave it asleep.
After an idle spell the Scan button needs a tap on the screen first.

## Scanning over USB

Works too, and for a lot of people it is the better option:

```ini
[panelbeater]
transport = usb        # or auto, which uses USB when the cable is there
```

**USB needs no enrolment, no host list and no Wi-Fi.** `SEND DIAGNOSTIC` has no
registration concept, so it works on a scanner straight out of the box — no
`panelbeater enrol`, and nothing to tap on the panel. If you just want the
button to work, plug the cable in and set `transport = usb`.

It also does resolution and colour mode, which the network path cannot:

```ini
resolution = 300       # or 150, 600
mode = color           # or gray, lineart
simplex = no
```

Known rough edge: each side is read until a 42 MB safety ceiling rather than
stopping cleanly at the end of the page, so a scan moves about 42 MB per side
over USB where the network path moves under 1 MB of ready-made JPEG. It works
and it is not slow in practice — nine seconds for a duplex sheet — but the
end-of-page detection is clearly not firing, and a page needing more than the
ceiling would be truncated.

Needs `pyusb`, plus Pillow and numpy to decode the image. Scanning is done
in-process rather than by shelling out to `scanimage`, because handing the
device over for the duration of a scan means being blind to the Stop button and
to errors.

**A USB cable disables the network path entirely.** This is not a preference:
with a cable attached the scanner refuses network registration (`-4`) for as
long as it is plugged in, so `transport = auto` is usually the right setting —
cable in, USB; cable out, network. It also means **dimming needs the cable
out**, since that only works on the network path.

**And it is mutually exclusive with SANE.** Only one process can claim
the USB interface, so while panelbeater is running `scanimage` fails at open
with "Invalid argument", and while a SANE scan is in progress panelbeater
cannot poll the button. If you need both, use `transport = network` and leave
USB to SANE.

You will probably need a udev rule to open the device without root:

```
# /etc/udev/rules.d/60-panelbeater.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="04c5", ATTR{idProduct}=="159f", MODE="0664", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="04c5", ATTR{idProduct}=="1632", MODE="0664", TAG+="uaccess"
```

## For protocol implementers

`docs/PROTOCOL.md` is the real output of this project: framing, discovery,
registration, enrolment, button notices, the scan sequence, status codes, and
the USB transport. It is written for someone implementing this somewhere else,
and it is explicit about which findings are tested and which are inferred.

It is GPL-2.0-or-later specifically so a SANE developer can lift code and
constants into sane-backends without a licensing question. There is a short
section at the end on what that would involve, including a one-line fix to the
existing `fujitsu` backend (`f1 09` should be `f1 04`) that stops the panel
hanging on "Scanning…" after a SANE-driven scan.

## Security

There is no authentication in this protocol. No pairing code, no confirmation
prompt: anything that can reach the scanner's TCP port can add itself to the
host list, take ownership of the panel, and scan. That is the vendor's design,
not a choice made here. Treat the scanner as you would a network printer and
keep it off untrusted networks.

## Licence

GPL-2.0-or-later. See `LICENSE`.

Reverse-engineered from packet captures for interoperability. No vendor code
was read or used. "ScanSnap" and "Fujitsu" are trademarks of their owners; this
project is not affiliated with or endorsed by them.
