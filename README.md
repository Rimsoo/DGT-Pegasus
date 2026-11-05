# Pegasus — play on lichess with a DGT Pegasus, on Linux

> [!NOTE]
> Main repository: [Forgejo](https://forgejo.aaaoz.fr/Rimso/DGT-Pegasus) · Mirror: [GitHub](https://github.com/Rimsoo/DGT-Pegasus) (synced automatically).
> Issues and feedback are welcome on GitHub.

DGT only ships a LiveChess `.deb` for Linux, and LiveChess cannot see a board
connected over Bluetooth: it enumerates serial ports. This project does without
both. It talks to the Pegasus directly over BLE and to lichess through its
Board API.

**No sudo, no device to create, no LiveChess.** Set up your pieces, play, the
opponent's move lights up, the clocks run.

The interface and its messages come in **English and French**: pick **EN** or
**FR** at the top right of the window, or pass `--lang en` / `--lang fr` on the
command line. The choice is saved in `~/.config/pegasus-bridge/settings.json`;
until you make one, it follows the system language. Below, button names are
given in English with the French label in italics.

<img width="882" height="724" alt="image" src="https://github.com/user-attachments/assets/e3ccffcb-0992-4c54-8bd9-5023e3c69f05" />


---

## What it does

* **Play on lichess from the board.** Your moves are sent automatically; your
  opponent's moves light up on the board until you play them back.
* **Start a game without touching the computer.** Lift two agreed-upon pieces
  and the game starts.
* **Everything you can't do by pushing a piece**: resign, draw, takeback, add
  time — six buttons.
* **Configurable light animations**: game start, win, draw, lifted piece, move
  accepted, opponent search.
* **LiveChess mode**, in an appendix, for those who want it: the bridge poses as
  a DGT board on a serial port (appendix A).

Three files are all it takes: `pegasus_bridge.py` (the engine and the command
line), `pegasus_gui.py` (the interface), `test_pegasus_bridge.py` (292 checks
that run without a board).

---

## Installation

### A. The AppImage — nothing to install

```bash
chmod +x build-appimage.sh && ./build-appimage.sh
./install.sh                       # ./install.sh -u to uninstall
```

The first script produces a single executable bundling Python, `bleak`, `chess`
and the interface. It downloads a manylinux Python image, so it needs network
access — once. Bluetooth goes through the system's BlueZ: nothing to bundle, but
`bluetooth.service` must be running.

The second installs without sudo: a `pegasus` command in `~/.local/bin`, the
icon in the theme, a menu entry. It delegates to
[shortcut](https://forgejo.aaaoz.fr/Rimso/shortcut) if it is on the PATH, and does
the same thing by hand otherwise.

### B. From source

```bash
sudo pacman -S --needed bluez bluez-utils python tk
sudo systemctl enable --now bluetooth

python -m venv ~/.venv/pegasus
~/.venv/pegasus/bin/pip install bleak chess
```

Python 3.9 or later. Tk is part of the standard library but often ships in a
separate package (`tk` on Arch, `python3-tk` on Debian) — it is only needed for
the interface, not for the command line.

The folder is also a regular Python package:

```bash
pip install .        # provides the pegasus-bridge and pegasus-gui commands
```

### The lichess token

A personal token with the **`board:play`** scope:

<https://lichess.org/account/oauth/token/create?scopes[]=board:play&description=Pegasus%20bridge>

Paste it into the interface and tick *remember* (*retenir*), or pass it once on
the command line with `--save-token`. It ends up in
`~/.config/pegasus-bridge/token`, mode 0600.

That one token covers everything: playing, seeking an opponent, challenging the
computer, resigning, offering a draw, taking back a move. Only the **+15 s**
button needs more (see below).

---

## Getting started

**With the interface:**

```bash
pegasus                              # installed AppImage
~/.venv/pegasus/bin/python pegasus_gui.py    # from source
```

*Search* (*Chercher*) → pick the board → *Connect* (*Connecter*). That's it.

**On the command line:**

```bash
pegasus play --address XX:XX:XX:XX:XX:XX --token lip_xxxxx --save-token
pegasus play --address XX:XX:XX:XX:XX:XX     # subsequent runs
```

Then start a game on lichess.org, or from the board: it is detected and
followed automatically. You can also join a game already in progress — the
bridge checks that the position matches, it doesn't make you replay everything.

---

## The interface

**The clocks** sit on the right: yours at the bottom, your opponent's above.
The side to move is shown in white, the other in grey, and it turns red under
twenty seconds. They tick down between lichess updates, including while you
think.

**The virtual board hides itself at the start of every game** — looking at it
would spoil the game. The *Show board* (*Afficher le plateau*) checkbox brings
it back when you need it, and unticks itself again at the next game. Outside a
game, it shows the starting position and marks in red what is still missing:
handy for setting the pieces back up before you start.

**The detail line** shows the opponent, your colour, the number of half-moves
and the board's **battery level**, refreshed every two minutes and marked
"charging" (*en charge*) when the cable is plugged in.

### Game buttons

Six buttons, active only during a game.

| button | what it does |
|---|---|
| **Draw** (*Nulle*) | offers a draw, or accepts the one offered to you |
| **Takeback** (*Reprise*) | asks to take back a move, or accepts the opponent's request |
| **+15 s** | adds fifteen seconds to *the opponent's* clock |
| **Resign** (*Abandonner*) | resigns — asks for confirmation |
| **Abort** (*Annuler*) | aborts the game, only possible before the first move |
| **Decline** (*Refuser*) | declines the pending offer, draw or takeback |

*Draw* and *Takeback* work both for offering and accepting: on lichess it is the
same endpoint.

**+15 s** is the odd one out. It goes through `/api/round/…` instead of
`/api/board/…`, which requires a different permission. With a `board:play`-only
token lichess will refuse, and the log will tell you to add `challenge:write`.
The other five work with the usual token.

### Starting a game from the board

Outside a game, with the board set up in the starting position: lift two
agreed-upon pieces, hold them up for a second, and the game starts. While
seeking, **a light circles in the middle of the board**. Putting the pieces back
cancels — lichess's API keeps the seek active as long as the connection is
alive, and closing it cancels the seek.

The **Edit gestures** (*Modifier les gestes*) button opens an editor: list on
the left, form on the right, with the squares to lift, the time control, the
game type and the colour. It refuses two gestures on the same squares. Gestures
are reloaded when the bridge starts — reconnect after changing them.

Three examples are written on first launch to
`~/.config/pegasus-bridge/gestures.json`:

```json
{
  "gestes": [
    { "nom": "Rapide 10+15 classée", "cases": ["a1", "h8"],
      "action": "seek", "minutes": 10, "increment": 15, "classee": true },
    { "nom": "Blitz 5+0 amicale", "cases": ["b1", "g8"],
      "action": "seek", "minutes": 5, "increment": 0, "classee": false },
    { "nom": "Ordinateur niveau 4", "cases": ["c1", "f8"],
      "action": "ai", "niveau": 4, "minutes": 10, "increment": 0,
      "couleur": "white" }
  ]
}
```

(The keys are in French: `gestes` = gestures, `nom` = name.)

| field | meaning |
|---|---|
| `cases` | the squares to clear; they must be occupied in the starting position |
| `action` | `seek` (human opponent) or `ai` (computer) |
| `minutes` / `increment` | the time control |
| `classee` | rated or casual game (`seek` only) |
| `niveau` | computer strength, 1 to 8 (`ai` only) |
| `couleur` | `white`, `black` or `random` |
| `variante` | `standard`, `chess960`, `antichess`, `atomic`… |
| `fourchette` | opponents' rating range, e.g. `"1500-1800"` |

`--no-gestures` disables all of it; `--gesture-delay` sets how long the pieces
must stay lifted (1.2 s by default — that delay is what lets you lift two
pieces without a shorter gesture firing along the way).

### Light settings

The **Lights** (*Lumières*) button. One speed slider and one duration slider per
event, a blink count where it makes sense, and a *Try* (*Essayer*) button per
row.

![Light settings](apercu_lumieres.png)

| event | duration = | blinks |
|---|---|---|
| game start | time lit | yes |
| game end | length of one flash | yes |
| opponent's move | pulse, **0 = stays lit** | — |
| piece lifted | pulse, **0 = stays lit** | — |
| move accepted | length of one blink | yes |
| move rejected | length of one blink | yes |
| seeking an opponent | time for one turn of the ring | — |

The sliders act **live**: the settings table is not copied, it is the very one
the current game uses. Move a slider, click *Try*, repeat — no need to wait for
the end of a game to see the end-of-game animation. *Start* and *End* have two
buttons each, so you can also see the black side and the draw.

*Save* (*Enregistrer*) writes to `~/.config/pegasus-bridge/leds.json`; *Factory
defaults* (*Valeurs d'usine*) restores the original settings without writing
anything.

The two lasting states — opponent's move, piece lifted — stay lit while their
duration is 0. Above that, they pulse at that rate until the situation changes,
and an animation that plays over them pauses the pulse, then lets it resume.

---

## What the board tells you

| moment | light signal |
|---|---|
| game start | your back rank lights up all at once — that's the side to sit on |
| seeking an opponent | a light circles in the middle, on the c3-c6-f6-f3 ring |
| you lift a piece | its square lights up, so you see where it came from |
| opponent's move | from and to squares light up until you've played it back |
| your move accepted | a short wink along the path, as soon as lichess validates it |
| your move rejected | fast, insistent pulse along the path |
| board needs fixing | the wrong squares light up |
| win | the winner's back rank lights up twice |
| draw | both back ranks at once, all sixteen squares |

### Who animates: the board, not us

The Pegasus does not light the list of squares it is sent: it **walks through**
it, square after square, at the rate given by the third byte of the frame.

```
60 0d 05 40 00 01 38 39 3a 3b 3c 3d 3e 3f 00
      │  │  │  │  └ the squares: a1 b1 c1 d1 e1 f1 g1 h1
      │  │  │  └ brightness
      │  │  └ repetitions
      │  └ walk rate  ← 64: so fast everything looks simultaneous
      │                   8: visible sweep; 2: far too slow
      └ "square sequence" subcommand
```

Ignoring this is costly. The first animations blinked a rank by switching it off
every 130 ms: each switch-off restarted the walk from the beginning, and only
the first square had time to show. Hence the "only a1 blinks" symptom, which had
nothing to do with the LEDs.

The right way is to send the list **once**, at the right rate, and leave it
alone. The order of the list sets the direction of the walk: black gets h8→a8 so
the light starts from their left just as white's starts from theirs, and the
draw interleaves both ranks (a1, h8, b1, g8…) to move two lights side by side.

`--led-speed N` forces the same rate everywhere, regardless of the settings;
`--led-style chase` makes the program animate instead of the board, in case some
firmware doesn't walk the list.

**One colour only.** Five parameters in the frame, none of them a colour. The
brightness byte remains a question mark: test 10 of the `led` probe tries it at
six intensities.

---

## Configuration files

Everything lives in `~/.config/pegasus-bridge/` (or `$XDG_CONFIG_HOME`).

| file | contents | edited by |
|---|---|---|
| `token` | the lichess token, mode 0600 | the *remember* checkbox, or `--save-token` |
| `gestures.json` | the start gestures | the *Edit gestures* button |
| `leds.json` | the light settings | the *Lights* button |
| `settings.json` | the interface language | the *FR / EN* selector, or `--lang` |

The first three each have an option to point elsewhere: `--token`, `--gestures`,
`--leds-file`.

---

## Command line

```
pegasus_bridge.py {scan,probe,watch,diag,raw,led,play,serve}
```

| subcommand | what it's for |
|---|---|
| `scan` | list BLE devices and spot the Pegasus |
| `probe` | check the link and see occupancy live |
| `watch` | show only sensor changes |
| `diag` | query the board: lock, authorisation, version |
| `raw` | raw console, send bytes by hand |
| `led` | probe the LEDs: chosen animations, variants, rates |
| `play` | **play on lichess** — the normal mode |
| `serve` | pose as a DGT board on a serial port (appendix A) |

The most useful `play` options:

| option | default | purpose |
|---|---|---|
| `--address` | — | the board's BLE address; without it, the board is searched for |
| `--token` | `$LICHESS_TOKEN`, then the file | the lichess token |
| `--save-token` | — | remember the token for next time |
| `--flip` | — | board rotated 180°, black in front of you |
| `--game` | — | join a specific game by its ID |
| `--promotion` | `q` | piece assumed on promotion |
| `--settle` | 0.15 s | settling time before reading a position |
| `--gesture-delay` | 1.2 s | how long the pieces stay lifted |
| `--no-leds` / `--no-gestures` | — | turn off lights or gestures |
| `--led-speed` / `--leds-file` | — | forced rate, other settings file |
| `--show-board` | — | show the board continuously in the terminal |
| `--lang` | system language | `fr` or `en` for the messages |
| `-v` / `--trace` | — | detailed log / every BLE notification |

`-v`, `--debug` and `--lang` are accepted before **as well as** after the subcommand.

---

## Troubleshooting

### 1. Calibrate — by far the most common cause

The Pegasus's detection is **inductive**, not magnetic: very short-range metal
detection. So a magnet proves nothing, and above all the board must be
**calibrated** — it does not happen by itself at power-on, despite what the
start-up LEDs suggest.

Without calibration, the board reports `01` on all 64 squares ("all occupied"),
never signals any change, and behaves exactly as if it were broken. **Calibrate
first, debug later.**

If the board stays silent, DGT publishes a factory reset: full charge, board
off and empty, then long-press on/off **until d4, d5, e4 and e5 blink rapidly
and simultaneously**. If those four squares don't blink, the sensor/LED board
isn't responding and it's a hardware fault.

### 2. The BLE link

```bash
pegasus_bridge.py scan
```

One line should read `<-- Nordic UART (Pegasus ?)`. Note the address, then:

```bash
pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v
```

The handshake should print the serial number and `firmware 1.x (major=1 ->
Pegasus confirmé)`, then the board updates when you move a piece. There's no
point going further until this step works.

### 3. The board responds but doesn't report occupancy

The handshake succeeds — serial number, firmware, trademark — but the dump
reports 64 occupied squares and no change ever arrives.

```bash
pegasus_bridge.py probe --address XX:XX:XX:XX:XX:XX -v --trace
pegasus_bridge.py raw --address XX:XX:XX:XX:XX:XX --led-test
```

| clue in the trace | cause | fix |
|---|---|---|
| constant `01` × 64 dump | board not calibrated (see 1), or board not unlocked | calibrate; `--handshake ext` |
| the occupied count **drops** when you add pieces | inverted encoding | `--invert` |
| frozen but plausible dump | the board pushes nothing on its own | `--poll 0.3` |

`--led-test` lights a rank for three seconds: physical proof that the board
receives your commands. If the LEDs light up, the problem is on the data side,
not the link.

Two initialisation sequences coexist in public implementations:

* `--handshake ext` — the Chrome extension's: developer key **as the very first
  write**, then `40`, `42`, `44`. This is the default, and the one that works on
  the Pegasus.
* `--handshake dgt` — the Dart driver `mono424/dgtdriver`'s: `40`, `45`, `4d`,
  *then* the key. If the board only accepts the key as the first write, this
  sequence returns a dummy board.

### 4. A gesture doesn't start a game

The log tells you why, once per situation:

```
gesture: the board isn't in the starting position: e4 should not be occupied
gesture: lifted squares: a1, d1 — no gesture matches (known: a1+h8 : …)
gesture: gestures are disabled (--no-gestures)
```

A gesture only fires if the board holds **exactly** the starting position minus
the gesture's squares: one piece left in the middle is enough to block
everything.

### 5. The board is upside down

If the white pieces appear at the top, use `--flip` (or the *Board rotated*
(*Plateau tourné*) checkbox in the interface). Don't confuse this with a
left-right swap: `--flip` rotates 180°, there is no mirror-only mode.

---

## Tests

```bash
python test_pegasus_bridge.py
```

292 checks, 34 sections, no hardware at all. They cover following a complete
game (castling, captures, en passant, promotion, piece in the air,
resynchronisation, takebacks), square orientation, reassembling BLE frames
split across several notifications, the serial protocol and DGT bus mode, the
light animations and their settings, start gestures, game actions, the
clock, and the English translation of every bridge message.

That's where most of this project's regressions were caught: if you touch the
code, run them.

---

## Appendix A — LiveChess mode (`serve`)

If you're attached to the LiveChess interface or its API, the bridge can pose as
a DGT board connected over serial. It's heavier — it needs `tty0tty` and `sudo`
— and the LEDs don't work, since LiveChess doesn't know the opponent's move. The
`play` mode is better in every way; this appendix is here for the record.

**A pty isn't enough.** A pseudo-terminal has no modem lines: the kernel handles
neither DTR, DSR nor CTS on `/dev/pts/*`, and `TIOCMSET` fails there. jSSC, which
LiveChess uses, sets DTR/RTS on open: it fails silently, opens the port and never
writes anything. Exact symptom: the port shows up in the list, LiveChess says
`Opening port`, and the bridge never receives a single byte.

**The solution is `tty0tty`**, a null-modem cable emulator that creates real,
paired kernel ttys with emulated modem lines:

```bash
yay -S tty0tty-dkms          # or make from github.com/lcgamboa/tty0tty
sudo modprobe tty0tty
ls -l /dev/tnt*              # tnt0<->tnt1, tnt2<->tnt3, ...

sudo ~/.venv/pegasus/bin/python pegasus_bridge.py serve \
     --address XX:XX:XX:XX:XX:XX --device /dev/tnt1 --link /dev/ttyUSB0 --debug
```

The name `tnt0` matches none of the patterns serial libraries recognise
(`ttyS`, `ttyUSB`, `ttyACM`, `rfcomm`…), so LiveChess wouldn't list it: `--link`
presents it under a suitable name. The bridge holds `/dev/tnt1`, opens up
permissions on both ends, and points `/dev/ttyUSB0` at `/dev/tnt0`.

**And above all, bus mode.** LiveChess is tournament software: it doesn't detect
boards in simple mode, but in DGT bus mode, the multi-board protocol. Its
detection sequence, as read with `--debug`:

```
40 40 40      reset
47            trademark        -> must contain "Digital Game Technology"
46            bus address      -> 2 bytes of 7 bits
4a            switch to bus mode
8b 00 00 0b   DGT_BUS_SEND_VERSION to address 0 (broadcast)
```

A board that doesn't answer that last frame doesn't exist for LiveChess, even if
everything else is perfect. The bridge implements full bus mode: ping, dump,
version, game start, changes and replay. Bus-mode commands are 4 bytes —
`[code|0x80, address MSB, address LSB, checksum]` — and are distinguished from
simple mode by bit 7.

Then: LiveChess at `http://localhost:1982`, then on lichess.org
*Preferences → DGT board*, which connects to its WebSocket.

Two details that are costly to rediscover: the trademark response **must**
contain `Digital Game Technology\r\nCopyright (c)`, and since the pty is
recreated every time the bridge starts, LiveChess must always be launched
*after* it.

---

## Appendix B — what we learned about the protocol

* **BLE**: Nordic UART Service `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`, write on
  `…0002`, notifications on `…0003`.
* **Developer key**: `63 07 be f5 ae dd a9 5f 00`, as the **very first write**.
  Without it, the board answers `7f` on all 64 squares; with it, `a5 01` and then
  real data.
* **Board → host frames**: `[id|0x80, lenHi, lenLo, payload…]`, total length
  `(lenHi<<7)|lenLo`, including the 3-byte header. A frame can be split across
  several notifications: it must be reassembled.
* **Square index**: 0 = a8 … 63 = h1, the vertical inverse of the usual
  convention (0 = a1). Mixing the two up gives a mirrored board that looks like
  swapped colours.
* **Occupancy only.** The board says which squares are occupied, never by what.
  Piece identity is reconstructed by following the game: stable occupancy
  compared against legal moves, at depth 1 then 2. Captures, castling, en
  passant and promotions are covered — and each one broke at least once before
  getting its test.
* **Battery** (message `0xA0`): percentage, hours and minutes remaining, on
  time, standby time, and a status byte whose bit 0 means "charging" and bit 1
  "on battery".
* **LEDs**: see "Who animates" above.

---

## What about chess.com?

There's no equivalent path. chess.com's public API is **read-only** — their
documentation says so in as many words: *"You cannot send game-moves or other
commands to Chess.com from this system"*. No Board API, no play token, no OAuth
for playing. The only official path for a Pegasus goes through the DGT mobile
app.

On desktop, the community works around it with a browser extension that reads
the position from the DOM and injects moves into it — that's what
[PegasusChessComChromeExtension](https://github.com/EdNekebno/PegasusChessComChromeExtension)
does, and it's where the BLE protocol used here comes from. It works, but it
breaks with every redesign of their interface, and automating the page goes
beyond what their terms of service allow.

---

## Credits

Pegasus protocol based on the public implementations
[BoardKit](https://github.com/fianchettochess/BoardKit) (MIT) and
[PegasusChessComChromeExtension](https://github.com/EdNekebno/PegasusChessComChromeExtension).
DGT serial protocol based on [PicoChess](https://github.com/jromang/picochess)
(GPL) and DGT's public documentation. Board API from
[lichess.org](https://lichess.org/api).
