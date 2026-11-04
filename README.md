# ViewMarq control without the ViewMarq software

`viewmarq.py` drives an AutomationDirect **ViewMarq** LED message display over Ethernet, using
plain Python. It needs no ViewMarq Configuration Software and no extra packages. It was built
against, and checked on, the **MD4-0212T** connected here: 2 rows × 12 characters, 72 × 16
tri-colour LEDs, firmware 1.66.

What it does:

* **Text** on row 1, row 2, or both, each with its own colour, alignment, blink and font.
  You can also mix colours inside a row, or use one big line across the full height.
* **Marquee**: scrolls automatically when text doesn't fit and stays still when it does.
  You can force it on or off.
* **Spill**: when you ask for it, text that doesn't fit on row 1 continues on row 2.
  Otherwise the two rows hold separate texts.
* **Unlimited length.** Up to about 1,850 characters per row scroll natively; the display
  loops them by itself, even after the program exits. Longer text is streamed to it in
  pieces while the program runs.
* **LEDs**: any LED on its own, several at once, sections (rows, halves, quadrants,
  rectangles, lines, character cells, border) or the whole board.
  * Colours: red, green and amber (orange).
  * Steady or hardware blinking at slow, medium or fast.
  * Also software flashing (N times, cycling colours) and "chase" (one LED at a time).
* **Finds the display by itself**, even though its address changes (see
  [Connecting](#connecting)).

Everything was verified on the real display (see *What was tested* at the end).
**PROTOCOL_NOTES.md** explains how the display works and how it was reverse engineered.

---

## Requirements

* Python 3.8 or newer. 3.14 is already installed on this PC. Only the standard library is
  used.
* The display on the same Ethernet network as the PC. Here it's a direct cable to the USB-C
  hub's Ethernet port ("Ethernet 3").
* Windows, macOS or Linux. Developed and tested on Windows 11.

Nothing to install. Open a terminal in this folder (`C:\path to your chosen clone destination\LTDB`) and run the
commands below.

## Quick start

```powershell
python viewmarq.py discover                     # list displays on every network adapter
python viewmarq.py text "Hello world!"          # text on the top row
python viewmarq.py text "Top row" "Bottom row"  # one text per row
python viewmarq.py leds "all red"               # every LED red
python viewmarq.py demo                         # tour of everything (~35 s)
python viewmarq.py laptimer start               # lap timer (see "Lap timer" below)
python viewmarq.py                              # interactive shell (type: help)
python viewmarq.py text --help                  # all options of a command
```

The first command after the display changes address takes a second or two longer, because the
program runs discovery. After that it connects instantly from the cached address.

---

## Text

Each quoted text goes on its own row: the first on row 1 (top), the second on row 2 (bottom).
Every `text` command replaces the whole screen, unless you use `--update`.

```powershell
python viewmarq.py text "TANK 1  240g"
python viewmarq.py text "TANK 1  240g" "TANK 2  220g" --color2 red
python viewmarq.py text "Line two only" --row 2
python viewmarq.py text "ALARM" --color red --blink fast --align center
```

| Option | Values | Notes |
|---|---|---|
| `--color` | `green` (default), `red`, `amber` (`orange`/`yellow` also work) | for all rows |
| `--blink` | `none` (default), `slow`, `medium`, `fast` | hardware blink |
| `--align` | `left` (default), `center`, `right` | static text only |
| `--font` | `normal` (5×7, default), `full` (5×8, room for descenders), `small` (5×5) | row fonts |
| `--marquee` | `auto` (default), `on`, `off` | see below |
| `--spill` / `--no-spill` | | see below |
| `--speed` | `slow`, `medium` (default), `fast` | the display has one scroll speed for all rows |
| `--direction` | `left` (default), `right` | scroll direction |
| `--pause N` | seconds | hardware "scroll in, pause N s, repeat" |
| `--update` | | change only the rows you give, keep the rest (see below) |

**Per-row options.** Add the row number to any of the options above, e.g. `--color2 red`,
`--align1 center`, `--marquee2 off`, `--blink1 fast`, `--font2 small`. Plain `--color` etc.
apply to every row.

**Colours inside a row.** Put tags in the text: `{red}`, `{green}`, `{amber}` (or `{orange}`),
blink tags `{slow}`, `{medium}`, `{fast}`, `{steady}`, and `{reset}` to go back to the row's
settings. Type `{{` and `}}` for literal braces.

```powershell
python viewmarq.py text "{red}ALARM {amber}pump 3 {green}OK"
```

**Characters.** The display shows printable ASCII. A backtick `` ` `` (or `°`) shows as a degree
sign, so `"25°C"` works. Curly quotes and dashes are converted, and other characters become the
nearest ASCII letter or `?`. `<` and `>` are fine.

**Capacity.** A row holds **12 characters** in the normal, full and small fonts.

### Marquee (scrolling)

* `--marquee auto` (the default) scrolls a row **only if its text doesn't fit**. Text that fits
  stays still, using the alignment you chose.
* `--marquee on` always scrolls, even short text.
* `--marquee off` never scrolls. Text that doesn't fit is cut off at 12 characters, unless it
  spills (below).

Each row decides for itself. For example, a long row 1 scrolls while a short row 2 stays put.

### Spill (text continuing onto the next row)

Spill is **off by default**, so two texts are two separate bodies of text. Turn it on with
`--spill`, giving **one** text:

```powershell
python viewmarq.py text "This text is far too long for one row of twelve" --spill
```

Row 1 gets as many whole words as fit (`This text is`) and stays still. Row 2 gets the rest. If
the rest still doesn't fit, row 2 follows the marquee setting: with `auto` it scrolls the
remainder, with `off` it is cut off.

### Big text (one line using both rows)

```powershell
python viewmarq.py text "BIG!" --big --font widefull --color red --align center
```

| `--font` with `--big` | Glyph | Characters across |
|---|---|---|
| `wide` (default) | 10×14 | 6 |
| `widefull` | 10×16 | 6 |
| `tall` | 5×14 (narrow) | 12 |

Choosing one of these fonts without `--big` switches to big mode automatically.

### Very long text ("unlimited length")

* **Up to about 1,850 characters on a single row**, or about 900 per row when both rows are
  long. The text is loaded into the display's 16 string variables and scrolls **natively**:
  it keeps looping on the display after the program exits.
* **Longer than that**: the program **streams** it. The display holds about 1,000 characters
  at a time; when one piece has scrolled through, the program loads the next. The program must
  keep running, and it says so. Press Ctrl+C to stop, or use `--loops N` to stop after N full
  passes.
  * There is a short blank gap between pieces, because each piece scrolls in from the right edge.
  * Piece timing uses the marquee speed. The defaults come from the firmware's timer tables
    (slow 45, medium 59, fast 83 LEDs/s).
  * On this display, medium was measured at 16.1 s for a 935-LED loop: **58.1 LEDs/s**, within
    about 1% of the default. This is saved in `.viewmarq.json`.
  * If pieces switch too early or too late at another speed, run
    `python viewmarq.py calibrate --speed slow` (or `fast`).

```powershell
python viewmarq.py text (Get-Content .\long_message.txt -Raw)
```

### Changing one row and keeping the other

```powershell
python viewmarq.py text "Status:" "running"
python viewmarq.py text --row 2 "STOPPED" --color red --update     # row 1 stays "Status:"
```

`--update` redraws the screen using the text this program last showed on the other rows. The
program remembers it in `.viewmarq.json`. The whole screen is redrawn rather than one row being
deleted in place: deleting a scrolling row in place froze this display's firmware during
testing.

---

## LEDs

Coordinates are in LEDs, with **x = 0–71 left to right** and **y = 0–15 top to bottom**. Row 1
is y 0–7 and row 2 is y 8–15. Each *selection* is a quoted phrase: **where**, then a **colour**,
then optionally a **blink rate**.

```powershell
python viewmarq.py leds "all green"
python viewmarq.py leds "top red" "bottom green fast"
python viewmarq.py leds "pixel 0 0 red" "pixel 71 15 amber"
python viewmarq.py leds "pixel 3 3 10 3 20 3 30 3 green"     # several LEDs in one selection
python viewmarq.py leds "rect 10 2 20 12 amber slow"
python viewmarq.py leds "all red" "rect 30 4 12 8 off"        # 'off' cuts a hole
```

| Selection | Meaning |
|---|---|
| `all` | whole board |
| `top`, `bottom`, `row N` | a text row (8 LEDs tall) |
| `left`, `right` | left / right half |
| `quadrant N` | 1 = top-left, 2 = top-right, 3 = bottom-left, 4 = bottom-right |
| `pixel X Y [X Y ...]` (also `led`, `p`) | one or more single LEDs |
| `rect X Y W H` | rectangle from its top-left corner and size |
| `box X0 Y0 X1 Y1` | rectangle from two corners |
| `col X [Y0 Y1]` | vertical line (full height by default) |
| `line Y [X0 X1]` | horizontal line (full width by default) |
| `cell R C` | one character cell: row 1–2, column 1–12 (6×8 LEDs) |
| `border` | outline of the board |
| `random N` | N random LEDs |

* **Colours:** `red`, `green`, `amber` (or `orange`), or `off` to turn LEDs in that area off.
* **Blink:** `slow`, `medium`, `fast`, or the word `blink` (= medium). Blinking is done by the
  display itself, so it keeps going after the program exits.
* `--color` and `--blink` set the defaults for selections that don't name one.
* Selections apply in order, so later ones paint over earlier ones.
* Everything not selected is dark.

**How much can be lit at once.** The display has no "set pixel" command, so the program builds
each picture from the display's own drawing tools:

* **One solid block of any size.** The display's LED-test fill draws it.
* **Up to 31 more pieces.** These are small solid blocks cut out of the built-in font glyphs
  with clipping windows.

A rectangle is cheap: often 1 piece, at most a dozen. Isolated single LEDs cost one piece each,
so about 31 scattered single LEDs (plus a block) fit on one screen. If a pattern needs more, you
get a clear error; show it in steps with `flash` or `chase`. Bigger pictures take a few tenths
of a second to draw.

### Flashing

`flash` turns the selection on and off a number of times:

```powershell
python viewmarq.py flash "all red" --times 5 --interval 0.5
python viewmarq.py flash "all" --colors red,green,amber --times 6 --interval 0.4
python viewmarq.py flash "left amber" "pixel 60 8 red" --times 3
```

For flashing that runs on its own, without the program, use a blink rate in `leds` instead,
e.g. `leds "all red fast"`. The display can't switch frames faster than about 10 times a second
(AutomationDirect asks for 100 ms between commands), so `--interval` has a minimum of 0.1 s.

### One LED at a time (chase)

```powershell
python viewmarq.py chase "row 1" --color green                  # every LED of row 1, one after another
python viewmarq.py chase "rect 0 0 12 8" --color red --fill     # light them up and leave them on
python viewmarq.py chase "line 7" --interval 0.2 --loops 3
```

---

## Lap timer

```powershell
python viewmarq.py laptimer start
```

The display shows the **current lap time** (`HH:MM:SS`) centred on the top row. The bottom row
shows **`HH:MM:SS ; N Laps complete`**: the previous lap's time and the laps done so far. That
line is longer than 12 characters, so it scrolls. Until the timer is started the top row shows
`00:00:00` blinking slowly.

| Keys | Action |
|---|---|
| **Ctrl+Shift+S** | start the timer |
| **Ctrl+Space** | lap: records the lap, adds it to the count, restarts the current lap at 00:00:00. Ignored until the timer is started. |
| **Ctrl+←** | current lap time **big** (both rows, centred). Press again for the normal view. |
| **Ctrl+→** | previous lap + laps completed **big** (scrolls, since it's long). Press again for the normal view. |
| **Ctrl+5** | finish: saves the CSV, shows **Standing by...** on the display, exits. Run `laptimer start` to go again. |
| Ctrl+C | same as Ctrl+5 |

The terminal shows a live status line and prints every lap. When you finish, it prints where the
log was saved, the best lap and the total.

**Logs.** Each run is saved in the **`laps`** folder next to `viewmarq.py` as **`MM-DD-YY-N.csv`**,
where N is 1, 2, 3, … for each run on that date (e.g. `11-03-26-1.csv`, `11-03-26-2.csv`).
There are two columns:

```
Lap,Lap Time
1,00:01:23.456
2,00:01:21.902
```

Times are recorded to the millisecond, taken the moment the key is pressed. The display shows
whole seconds. The lap that is still in progress when you press Ctrl+5 isn't counted. A run with
no laps still gets a file (header only).

**Details**

* Laps closer together than **1 second** are ignored, so a double press can't add a false lap
  (`--min-lap` changes this). Holding the keys down doesn't repeat.
* The keys work **while this terminal window is focused**. Add `--global` to make them work from
  any window. While the timer runs, those five combinations then don't reach other programs; for
  example, Ctrl+←/→ won't jump words in an editor.
* The timing runs on the PC. If the display or cable has a problem, laps are still timed and
  recorded, and the display catches up when it's back.
* After every lap the laps so far are also written to `laps\_in_progress.csv`. If a run is cut
  off without Ctrl+5 (window closed, power loss), the next `laptimer start` turns that file into a
  normal numbered log and tells you.
* `python viewmarq.py laptimer standby` just puts the standby message on the display.
* Options:

  | Option | Default |
  |---|---|
  | `--color-current` | green |
  | `--color-previous` | amber |
  | `--standby-text` | "Standing by..." |
  | `--speed` | medium; how fast the long line scrolls |
  | `--out FOLDER` | `laps`; where the CSVs go |

## Interactive shell

Run `python viewmarq.py` (or `python viewmarq.py shell`). It accepts the same commands as above
without the `python viewmarq.py` prefix. It also keeps settings that act as defaults until you
change them; this is the easiest way to toggle the marquee and spill.

```
viewmarq> text "Hello" "World"
viewmarq> marquee off
viewmarq> spill on
viewmarq> text "A long sentence that now wraps instead of scrolling"
viewmarq> color red
viewmarq> leds "pixel 5 5" "rect 20 0 10 16 amber fast"
viewmarq> settings          (shows: color blink align marquee spill font bigfont speed direction)
viewmarq> help
viewmarq> quit
```

## Other commands

| Command | What it does |
|---|---|
| `discover` | lists every ViewMarq visible from any network adapter (name, model, IP, MAC, id, DHCP, orientation) |
| `info` | model, firmware, name, MAC, address, id, options, orientation, LED geometry |
| `clear` | blanks the display |
| `raw "<CLR><RED><T>Hi</T>"` | sends a raw ViewMarq ASCII command (adds `<ID n>`) and prints the reply; see PROTOCOL_NOTES.md |
| `setvar N "text"` | sets string variable N (1–16), for screens that use `<STR N len>` |
| `demo [--delay S]` | walks through every feature (`--delay 0` waits for Enter between steps) |
| `calibrate [--speed S]` | measures the real marquee speed: press Enter each time `>>START<<` comes round |
| `calibrate --clock [--seconds N]` | the same, but row 2 shows a running clock; note it each time `>>START<<` reappears, then save with `calibrate --period P` (P = seconds per loop) |
| `orientation [auto\|normal\|inverted] --yes` | shows or sets the auto-flip behaviour (see below) |
| `netconfig --static IP [--mask M --gateway G] --yes` / `netconfig --dhcp --yes` | sets the display's IP address |

Global options go **before** the command:

| Option | What it does |
|---|---|
| `--ip` | skip discovery |
| `--local-ip` | the PC address to use |
| `--mac` | pick one of several displays |
| `--dry-run` | print commands instead of sending them; no display needed |
| `--preview` | draw the result in the terminal |
| `-v` | print every command sent and the display's reply |

```powershell
python viewmarq.py --dry-run --preview text "Hello" "World" --align2 right
python viewmarq.py -v leds "border amber"
```

## Orientation (auto-flip)

The display has an accelerometer. By default (**auto**) it turns everything right-side up when
it is mounted the other way round. This includes text, LED coordinates and scrolling direction,
so nothing in the program has to change: (0, 0) is always the top-left LED as you look at it.

`python viewmarq.py info` shows the current mode. To lock it, for example on a vibrating machine
where it might flip back and forth, run one of:

```powershell
python viewmarq.py orientation normal --yes     # lock to the display's factory orientation
python viewmarq.py orientation inverted --yes   # lock to the factory orientation turned 180°
python viewmarq.py orientation auto --yes       # back to automatic (the factory setting)
```

**"normal" and "inverted" are relative to the display's factory orientation, not to how you see
it.** This board is currently mounted the other way up, and auto mode corrects for that. So on
this board, as mounted now:

* `inverted` reads right-side up
* `normal` shows everything upside down

If you lock it, pick whichever reads correctly where it's mounted; `auto` always does.

Each change rewrites the display's saved settings; the display restarts (it's back in about 7 s)
and the program checks the new setting took effect. Without `--yes` it only explains what it
would do.

**Tested on this unit (29 Oct 2026):**

| Setting | Options word | What you saw |
|---|---|---|
| inverted | 0x0E03 | text right-side up |
| normal | 0x0A03 | text upside down |
| auto | 0x0803, the original | right-side up |

The static IP was kept through every change. The display is **left on auto**.

## Connecting

You don't need to give an address. The program:

1. **Finds the display.** It sends the same UDP broadcast the ViewMarq software's *Browse*
   button uses (port 9560) from **every** network adapter, and the display answers with its
   name, model, MAC and current IP.
2. **Connects with Modbus TCP** (port 502) **from the adapter that heard it.** This matters on
   this PC: five adapters have 169.254.x.x addresses and Windows sends that whole range into
   the **Tailscale** tunnel. A normal connection to the display therefore times out even
   though `ping -S` works.
3. **Remembers** the address in `.viewmarq.json` next to the script, so the next run connects
   instantly. If that address stops working, it runs discovery again.

**Why the address changes and why "Acquiring IP" appears.** The display is in its factory
**DHCP** mode. There is no DHCP server on a direct cable, so it gives itself a self-assigned
169.254.x.x address. Every so often it tries DHCP again. For about a minute it then shows
"Acquiring IP", has no address at all (discovery reports 0.0.0.0), and can't be reached. It
usually comes back with the same address, but after a power cycle it came back as
169.254.205.70 instead of 169.254.205.169.

The program waits these periods out: it keeps rediscovering the display by MAC, reconnects and
carries on.

**To get rid of the dropouts**, give the display a static address. AutomationDirect recommends
this for permanent installations. Keeping it in the 169.254 range means the PC's settings don't
have to change:

```powershell
python viewmarq.py netconfig --static 169.254.205.70 --mask 255.255.0.0 --yes
```

This uses the display's own "set configuration" broadcast and addresses it by MAC, so it works
whatever its current IP is. It changes saved settings and restarts the display.
`netconfig --dhcp --yes` (or the official software, or a factory reset) undoes it.

**Done on this unit (29 Oct 2026).** The display is now **static 169.254.205.70 / 255.255.0.0**.
It restarted and was reachable again within 10 s, and its saved settings read back with options
0x0803 (DHCP off).

## Using it from your own Python code

```python
import viewmarq as vm

dev = vm.open_display()                    # discovery + connection, as the CLI does
g = dev.geometry                           # 72 x 16, 2 rows

frame = vm.build_text_frame(g, dev.display_id, {
    1: vm.RowRequest("Line 1 {red}hot", color="green"),
    2: vm.RowRequest("This long line scrolls by itself", color="amber"),
})
vm.send_frame(dev, frame)

pixels = vm.paint(["top red", "pixel 10 12 green fast"], g)
vm.send_frame(dev, vm.build_led_frame(g, dev.display_id, pixels))

dev.send(f"<ID {dev.display_id}><CLR><AMB><T>raw command</T>")   # returns "OK" or an error
dev.set_string_var(1, "updated value")
dev.clear()
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| `no ViewMarq display found` | Check the cable and that the adapter is enabled. If the display shows "Acquiring IP", wait a minute. Run `python viewmarq.py discover`. |
| Messages "re-acquiring its IP address ... waiting" | Normal in DHCP mode (see above). It continues by itself. A static IP stops it. |
| The display shows a frozen picture and doesn't respond | Power-cycle it. It happened once in testing, after a row was deleted in place while scrolling; the program no longer does that. If it recurs, note what you ran. |
| `display rejected the command: E..` | The display's own error code. The table is in PROTOCOL_NOTES.md. `-v` shows the exact command. |
| `that pattern needs N display elements` | Too many separate LED groups for one screen; simplify, or use `flash`/`chase`. |
| Streamed text switches pieces too early or late | `python viewmarq.py calibrate --speed medium` (or the speed you use). |
| You use the ViewMarq software at the same time | That's fine. Both can talk to the display, but don't send from both at once. |

## Files

| File | Contents |
|---|---|
| `viewmarq.py` | the program: CLI, interactive shell and importable library |
| `README.md` | this guide |
| `PROTOCOL_NOTES.md` | findings: network, discovery, Modbus registers, command language, fonts, limits, timing, and how each was found |
| `.viewmarq.json` | created automatically: last display address, last text screen (for `--update`), calibrated speeds. Safe to delete. |
| `laps\MM-DD-YY-N.csv` | one lap log per lap timer run |

## What was tested on the real display

Each of these was sent to the MD4-0212T, accepted ("OK"), and the visual result was confirmed by
looking at the display:

* four quadrants in red / green / amber / red, all solid
* single LEDs in all four corners plus two adjacent centre LEDs, exact positions, each blinking
  at a different hardware rate
* "HELLO WORLD!" filling the 12-character top row without scrolling
* a centred three-colour bottom row
* spill: row 1 static, the rest scrolling smoothly on row 2
* a scrolling row made of text plus two string variables, continuous
* big full-height blinking text

Every other screen in `demo` was sent and accepted without error:

* whole board in each colour, halves, border and cell
* chase, chase-and-fill, and software flash with colour cycling
* 1,850-character native marquees on one row and 900 characters on each of two rows
* a streamed 3,000-character text across piece switches
* `--update`, right-to-left scrolling, the small and full fonts, and `<` `>` `°` in text
* the interactive shell

`netconfig --static` and `orientation` (inverted, normal, then back to auto) were also run on the
device and confirmed by looking at it.
