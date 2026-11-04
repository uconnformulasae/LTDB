# ViewMarq MD4-0212T: protocol notes and findings

These notes cover what was learned about the display and how. Each fact is tagged with its
source:

* **[doc]**: AutomationDirect documentation (the help file shipped with the software, or the
  official spec sheets)
* **[sw]**: disassembly of `ViewMarq.exe` / `vmcomm.dll` (software 2.06)
* **[fw]**: disassembly of the firmware image `VM-A1016700.vmf` (1.67, shipped with the software)
* **[test]**: measured on the real display: status replies, plus what was seen on the LEDs

The unit tested is an **MD4-0212T**: MAC 60:52:D0:05:CD:79, factory name "Display_CD79",
ASCII/Modbus id 1, **firmware 1.66**. (The software ships 1.67 but it was not installed.)

## 1. Method

1. **Installed software.** `C:\Program Files (x86)\AutomationDirect\Viewmarq Configuration Software\`
   contains:
   * `ViewMarq.exe`: the 32-bit MFC application, including a full message simulator.
   * `vmcomm.dll`: a generic Modbus RTU/TCP master.
   * `viewmarq.chm`: the help file.
   * `readme.pdf`
   * `Firmware\VM-A1016700.vmf`: the firmware image.

   The help file was decompiled with `hh.exe -decompile` and read in full (35 pages, including
   the ASCII command appendix, the Modbus register map and every PLC connection example).
2. **Network.** The display is on the USB-C hub's Ethernet ("Ethernet 3", Realtek USB GbE, PC
   address 169.254.129.68/16). The display started at 169.254.205.169.
3. **Software analysis** with Python `pefile` and `capstone`, in a throw-away virtual
   environment:
   * the socket imports and every `htons` / `bind` / `sendto` call (discovery protocol)
   * the packet builders
   * the configuration state machine and its Modbus request tables
   * the simulator's command parser, i.e. the jump table over the command-token table
   * the `<MTN>`, `<CLRXY>`, `<CLRWIN>` and `<WIN>` handlers
   * the simulator's timing tables
4. **Firmware analysis.** The image is unencrypted Motorola ColdFire/68k code, loaded at file
   offset + 0x15128 (derived from the pointers in its command table). From it:
   * every command keyword
   * every error message
   * the `<MTN>` handler, disassembled with capstone's M68K mode
   * the timing tables
   * all font bitmaps, extracted
5. **Probing the display.**
   * Read-only Modbus reads of every documented register.
   * Command strings, checking the reply the display leaves in its status buffer. This
     measured the limits.
   * A visual check of each drawing technique, done by the user looking at the board.
6. **Official web documentation**, read for physical specs: the 2-line spec sheet (LED pitch
   5 mm, 7 mm centre spacing, tri-colour red/amber/green) and the character list appendix.

## 2. Network behaviour

* **Factory setting is DHCP** [doc][test] (option bit 0x0020). Without a DHCP server the
  display uses **APIPA** (169.254.x.x / 255.255.0.0).
* **"Acquiring IP"** [test]. In DHCP mode the display periodically tries DHCP again. For roughly
  a minute it then:
  * shows "Acquiring IP" if the startup banner is on screen
  * has **no IP address**: it stops answering ARP and TCP
  * still answers discovery broadcasts, reporting IP **0.0.0.0**

  It then falls back to an APIPA address, usually the same one. **After a power cycle it picked
  a different one** (169.254.205.169 became 169.254.205.70).
* **Routing trap on this PC** [test]. Five adapters hold 169.254.x.x/16 addresses, and Windows
  routes 169.254.0.0/16 through **Tailscale** (interface metric 5, against 35 for Ethernet 3).
  An ordinary TCP connection to the display therefore goes into the VPN tunnel and times out.
  The fix is to **bind the socket to the Ethernet adapter's address**.
  `viewmarq.py` does this automatically, using the adapter that received the discovery reply.
* **Modbus TCP** server on port 502 [doc][test]. Unit id 255 works; AutomationDirect's PLC
  examples use 255. Supported function codes: 03, 06, 16 [doc].
* **Idle timeout**: 30 s [test]. The display closes idle TCP connections, so clients must
  reconnect.
* "Modbus TCP is the only protocol available for Ethernet" [doc]. The ASCII command language
  travels *inside* Modbus registers.

## 3. Discovery protocol (UDP 9560) [sw][test]

The software's *Browse* button broadcasts to 255.255.255.255:**9560** and listens on 9560. The
display **replies by unicast to the sender's port**, so any local port works [test]. That means
no conflict with the ViewMarq software. Every packet starts with a 9-byte header:

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | ASCII `MATX` |
| 4 | 1 | version `01` |
| 5 | 2 | transaction id `00 00` |
| 7 | 1 | length: number of bytes from offset 8 to the end |
| 8 | 1 | function (a reply sets bit 7) |

**Function 1, discovery query** (9 bytes): `4D 41 54 58 01 00 00 01 01`

**Function 0x81, discovery reply** (66 bytes, length 0x3A), all big-endian:

| Offset | Size | Field | This unit |
|---|---|---|---|
| 9 | 6 | MAC | 60 52 D0 05 CD 79 |
| 15 | 4 | IP | 169.254.205.169 (0.0.0.0 while acquiring) |
| 19 | 4 | subnet mask | 255.255.0.0 |
| 23 | 4 | gateway | 0.0.0.0 |
| 27 | 2 | option flags (same word as Modbus 445109) | 0x0823 |
| 29 | 16 | name, NUL padded | "Display_CD79" |
| 45 | 1 | ASCII / Modbus id | 1 |
| 46 | 16 | model | "MD4-0212\0T4-0212" (see below) |
| 62 | 2 | Modbus TCP port | 502 |
| 64 | 2 | TCP idle timeout (s) | 30 |

The model field holds "MD4-0212T" damaged by an overlapping copy in the firmware. The same bytes
appear over Modbus. `viewmarq.py` repairs it.

**Function 2, set configuration** (50 bytes, length 0x2A): this is what the *Ethernet Browse
Editor* sends. It addresses the display **by MAC**, so it works across subnets. The layout is the
reply's minus the model field:

```
header(fn 2) MAC(6) IP(4) mask(4) gateway(4) flags(2) name(16) id(1) tcp-port(2) idle-timeout(2)
```

The display "automatically resets and restarts" after an update [doc]. Clearing flag 0x0020
selects a static IP. This is implemented as `viewmarq.py netconfig`.

**Verified on the unit** [test]: this 50-byte packet set static 169.254.205.70 / 255.255.0.0,
gateway 0.0.0.0, flags 0x0823 → 0x0803:

```
4d415458 01 0000 2a 02 6052d005cd79 a9fecd46 ffff0000 00000000 0803
446973706c61795f4344373900000000 01 01f6 001e
```

The display sent an acknowledgement and answered discovery as "static" within 7 s. Modbus
445200 then read 169.254.205.70 / 255.255.0.0 and 445109 read 0x0803. The whole flags word is
applied, so the orientation bits could probably be set this way too, but that was not tried.

**Function 3** (17 bytes): MAC(6) plus a 16-bit value. It is sent by the *Blink LED Display*
button, i.e. it identifies a display. Not used.

## 4. Modbus register map

**Addressing** [doc][test]. Register 4xxxxx is at zero-based Modbus address **xxxxx − 1**, so
411000 is address 10999. (The DirectLogic help page says so explicitly; verified by reading the
model string.)

**String byte order** [test]. Each register carries **the first character in its low byte**,
e.g. "MD" is stored as 0x444D. This is the display's default, with option "Byte Swap Strings"
(0x0080) off; the P3000 help page tells users to tick "Byte Swap" for the same reason. Numeric
registers are normal big-endian.

| Register | Words | Contents | Notes |
|---|---|---|---|
| 400100 | 2×32 | numeric variables 1–32 (high word, low word) | [doc] |
| 400200 | 50×16 | **string variables 1–16**, 100 chars each, at 400200 + 50·(n−1) | [doc][test] |
| 411000 | 256 | **command buffer** (512 bytes) | [doc][test] |
| 411500 | 256 | **status / reply buffer** | [doc][test] |
| 445000 | 10 | model string | [doc][test] |
| 445010 | 10 | ASCII version string ("A1  1.66") | [test] |
| 445020 | 10 | firmware version string ("FW  1.66") | [test] |
| 445050 | 3 | MAC address | [test] |
| 445100 | 8 | display name (string) | [doc][test] |
| 445108 | 1 | heartbeat timeout (s), 0 = off | [doc][test] |
| 445109 | 1 | **option flags** (table below) | [doc][test] |
| 445110 | 1 | Modbus TCP port | [test]; the doc mislabels it "(1-247)" |
| 445111 | 1 | TCP idle timeout (s) | [test] |
| 445112 | 1 | ASCII id | [test] |
| 445113 / 445114 | 1 / 1 | 200 / 500: constants the software writes back | [sw][test] |
| 445200 | 8 | static IP, mask, gateway, DNS (2 words each); all 0 while in DHCP mode | [doc][test] |
| 445300 | 9 | port 1 (RS-232) settings | [doc] |
| 445400 | 7 | port 2 (RS-485) settings | [doc] |

**Option flags** (445109 and the discovery flags word; this unit is 0x0823):

| Bit | Meaning | Source |
|---|---|---|
| 0x0001 | display startup banner (set) | [doc] |
| 0x0002 | undocumented (set on this unit) | — |
| 0x0008 | display heartbeat error on comms loss | [doc] |
| 0x0010 | disable ASCII command reply on serial | [doc] |
| 0x0020 | DHCP (set) | [doc] |
| 0x0040 | word swap user variables | [doc] |
| 0x0080 | byte swap strings | [doc] |
| 0x0100 | diagnostic LED | [doc] |
| 0x0200 | **orientation fixed** (clear = automatic, accelerometer) | [doc] |
| 0x0400 | **fixed upside down** | [doc] |
| 0x0800 | display syntax errors (set) | [doc] |

**Writing configuration** [sw]. The help calls the 445xxx registers read-only. The software's
*Write Configuration* is a seven-state machine (jump table at 0x40DF2C; request table built at
0x40F7xx); its buffers are built at 0x40EB40. It writes these undocumented registers, in order,
each with the same layout as the block it mirrors:

1. **412100** ×15 ← block 445100: name, heartbeat, flags, port, timeout, id, 200, 500
2. **412200** ×8 ← block 445200
3. **412300** ×9 ← block 445300
4. **412400** ×7 ← block 445400
5. **465500** ← `FFFF 0008`
6. **465500** ← `FFFF 03E3 9289` (commit; the display saves and restarts)

`viewmarq.py orientation` reads the four live blocks, changes only bits 0x0200 / 0x0400 and
replays this sequence.

**Verified on the unit** [test]. The blocks read before writing were:

| Block | Value |
|---|---|
| global | name "Display_CD79", 0, 0x0803, 502, 30, 1, 200, 500 |
| network | 169.254.205.70 / 255.255.0.0 / 0 / 0 |
| port 1 | 0002 0001 00009600 0001 0008 0 0 0 (ASCII, id 1, 38400, odd, 8 bits) |
| port 2 | 0001 0001 00009600 0001 0008 0 (Modbus, id 1, 38400, odd, 8 bits) |

After the two commit writes the display restarted, answered discovery again within about 7 s,
and read back exactly the new options word. This was done three times (0x0E03, 0x0A03, 0x0803),
and the network and serial settings were unchanged each time.

The software reads configuration from 445100 ×15, 445200 ×8, 445300 ×9 and 445400 ×7. It moves
command strings in 100 / 100 / 56 register pieces.

## 5. Sending commands

The display is a slave. A **command string** is ASCII written into the command buffer:

1. The string must contain `<ID n>` (matching the display's id; 0 = broadcast) and must **end
   with CR (0x0D)** [doc]. Anything after the CR is ignored.
2. **Maximum 511 characters + CR = 512 bytes** [test]. A write past register 411255 fails with
   Modbus exception 2.
3. A string may be written in several Modbus writes. The display waits for the terminating CR,
   and stops at a NUL until it is replaced [doc]. `viewmarq.py` writes **the tail first and the
   head last**: the display clears the first register after each command, so it can never act
   on half a string.
4. When the display has processed the string it **clears register 411000 to 0** [doc][test]. It
   then leaves its reply in **411500**: "OK" or an error such as "E21: Syntax error at pos 7".
   Processing took 20–60 ms in testing.
5. **Leave ≥ 100 ms between command strings** [doc].
6. Attributes (font, colour, blink) carry over between text blocks **within one command
   string** [doc]. The program re-states them at the start of every string.

The reply buffer only says whether the string parsed. For example, `<MTN 4>` and `<POS 72 0>`
both return OK [test]. It cannot confirm what is drawn, which is why the drawing techniques were
checked visually.

## 6. The command language

Documented [doc]: `<ID n>`, `<CLR>`, `<T>…</T>`, `<WIN x0 y0 x1 y1>`, `<POS x y>`, `<OFF x y>`,
`<CS n>`, `<GRN>`, `<RED>`, `<AMB>`, `<BL N|S|M|F>`, `<LJ>`, `<CJ>`, `<RJ>`,
`<SL|SR [LJ|CJ|RJ secs]>`, `<SU|SD [TOP|BOT secs]>`, `<S S|M|F>`, `<STR n len>`,
`<SETS n>…</SETS>`, `<DEC n len dp>`, `<DECZ …>`, `<SETV n value>`, `<MTN …>`, `<DIAG n>`,
`<VER>`, `<NAME>`, `<IPADDR>`, `<PORTPAR n>`, `<STATUS>`, `<VERSION>`, `<CLRWIN x y>`,
`<DO n>` (message list).

The firmware's full keyword table [fw] (index order at file offset 0x2ED08, the same in the
simulator at 0x4F36F0): `<T>` `<CS` `<WINX` `<WIN` `</WIN` `<CLRXY` `<CLRWIN` `<CLR` `<STR`
`<DECZ` `<DEC` `<BL` `<PGM` `<MTN` `<SETS` `<SETV` `<STATUS` `<LJ` `<RJ` `<CJ` `<SL` `<SR` `<SU`
`<SD` `<SW` `<S` `<FPGA` `<TR` `<DO` `<DL` `<ID` `<DS` `<IPADDR` `<PORTPAR` `<VERSION` `<VER`
`<POS` `<OFF` `<DIAG` `<RED` `<GRN` `<AMB` `<UPD` `<NAME`.

Notes on the undocumented ones:

* `<WINX>`: like `<WIN>` but not clamped to the panel [sw].
* `<CLRXY x y>`: deletes the element whose window origin plus offset is (x, y) [sw][test].
* `<CLRWIN x y>`: deletes the elements whose window starts at (x, y); "E90: Nothing to Clear"
  if there are none [sw][test]. **Using it on a row that was scrolling froze firmware 1.66**
  (observed once [test]). The display stopped drawing and stopped answering the network until it
  was power-cycled. A static window cleared fine. `viewmarq.py` never uses it.
* `<TR n>`: an attribute with 4 values. `<SW>`: an effect, alongside `<SL>` etc. `<UPD>`: parsed
  like `<POS>`.
* `<PGM n>`, `<FPGA n>`, `<DL>…</DL>`, `<DS n>`: programming, FPGA and message-list download
  commands. They were **deliberately not sent**.

**`<MTN mode X Y W H>`** (the LED test command) [doc][fw][test]:

* Modes: 0 = solid green, 1 = solid red, 2 = solid amber, 3 = advanced test pattern; 4 is
  accepted. A size of 0 means "to the window edge".
* Firmware routine at file offset 0x2BFDC (address 0x41104). It parses the arguments, calls the
  same clear routine as `<CLR>` (0x41080), then writes one element and marks it as the **end of
  the display list**. It picks up the current `<BL>` blink period.
* Result: **one solid rectangle per screen, of any size, in any colour, optionally blinking**.
* **It must be alone in its command string** [test]. Elements that follow it in the same string
  are stored (they count against the limit) but **never drawn**. Elements sent in *later*
  strings are appended and drawn normally.

**Errors** [fw]. E1–E25 are listed in the help. The firmware also has:

* E4: invalid MTN value
* E15: invalid CLR format
* E90: nothing to clear
* E91: no text storage
* E92 / E93: off display
* E94: NULL status
* E95: unsupported command
* "Error: `<OFF>` only valid after `<WIN>`"

## 7. Limits measured on the display [test]

| Limit | Value |
|---|---|
| Elements per screen (each `<T>` block / `<STR>` / `<DEC>`) | **31**; the 32nd gives "E6: Display Memory Full" |
| `<MTN>` block | does not count; 1 MTN + 31 text elements is accepted |
| Elements across several command strings | accumulate (20 + 11 OK, then E6) until `<CLR>` |
| Literal text characters | 1500 ("E7: Text Memory Full") [doc][fw] |
| String variable text | **does not count** toward the 1500. 16 × 100 = 1600 plus 280 literal (1880 characters) in one scrolling row was accepted. |
| `<STR n len>` | len ≤ 100 ("E16: Invalid STR/DEC Size") |
| `<CS n>` | n = 0–10 accepted, 11 gives "E10: Parameter Out of Range" |
| `<`, `>` inside `<T>…</T>` | fine; only the sequence `</T>` ends the block |

String variables can be set with `<SETS n>…</SETS>` or by writing registers 400200 + 50·(n−1)
directly. Both were verified by reading the registers back. Several `<STR>` elements chained in
one scrolling window scroll as one continuous line [test].

## 8. Display geometry, fonts and LED stamps

* The simulator sizes displays as panels_x × 72 by panels_y × 8 LEDs [sw]. The MD4-0212T is
  **72 × 16**: 2 text rows of 8 LEDs, and 12 characters of 6 LEDs [doc][test]. (0, 0) is the
  top-left LED.
* Fonts are **fixed width**: width = character count × cell advance [sw]. The firmware stores 11
  character sets as plain bitmaps, 96 glyphs each (codes 32–127, with `` ` `` drawn as °), one
  to three bytes per row, most significant bit on the left [fw]:

| CS | Name in viewmarq.py | Glyph | Advance | Firmware offset | Help name |
|---|---|---|---|---|---|
| 0 | normal | 5×7 | 6 | 0x1EEFE | 2 inch, 1 line |
| 1 | full | 5×8 | 6 | 0x1F19E | 2 inch full line |
| 2 | small | 5×5 | 6 | 0x1ED1E | 1¼ inch compact |
| 3 | tall | 5×14 | 6 | 0x1F49E | 4 inch narrow |
| 4 | wide | 10×14 | 12* | 0x1F9DE | 4 inch |
| 5 | widefull | 10×16 | 12* | 0x2045E | 4 inch full line |
| 6–8 | — | 10×21, 15×21, 15×28 | — | 0x2105E, 0x2201E, 0x22FDE | 6 / 8 inch |
| 9–10 | — | 20×28, 20×32 | — | packed differently, not decoded | 8 inch |

\* The advance of CS 0–3 is confirmed by "12 characters per row". For CS 4/5 the true advance
is 11 or 12. Either way exactly 6 characters fit across 72 LEDs, so the fit and marquee
decisions are the same. CS 6–10 are taller than this display.

**LED stamps.** There is no "set pixel" command, so `viewmarq.py` builds LED patterns from three
pieces:

* one `<MTN>` block for the largest rectangle
* **glyph stamps** for everything else: a `<WIN>` window clips a built-in glyph that contains a
  solid block of lit LEDs, and `<POS>` (negative values allowed) moves the block into the window
* the solid blocks, found by searching all glyph bitmaps of CS 0–8 (Pareto-best set):

| Stamp | Font, character | Block position in glyph |
|---|---|---|
| 2×2 | CS2 '%' | (0,0) |
| 5×1 | CS2 '#' | (0,1) |
| 1×8 | CS1 '1' | (3,0) |
| 4×4 | CS4 'X' | (3,5) |
| 10×1 | CS4 '2' | (0,13) |
| 9×2 | CS5 '#' | (1,5) |
| 2×16 | CS5 '1' | (4,0) |
| 3×28 | CS8 '$' | (6,0) |
| 7×7 | CS8 '8' | (4,10) |
| 4×12 | CS8 '0' | (0,8) |
| 15×3 | CS7 '2' | (0,18) |
| 11×4 | CS8 'R' | (0,12) |

Each rectangle is tiled with whichever stamp needs the fewest elements. The visual checks [test]
confirmed:

* solid quadrants built from CS8 'R' stamps placed at negative y
* single LEDs placed exactly at the corners and at (35,7) and (36,8)
* per-stamp hardware blink

## 9. Timing [fw][sw]

| Table | Values | Use |
|---|---|---|
| Blink period | none / slow / medium / fast = 0 / 40 / 20 / 10 ticks | `<BL>` |
| Scroll step | 22 / 17 / 12 ms per LED for slow / medium / fast | simulator timer table at 0x5283D8, matching firmware constants at 0x43C78–0x43C98 |
| Command gap | ≥ 100 ms between command strings [doc] | 55 ms between packets to different serial slaves |

The scroll steps give roughly 45 / 59 / 83 LEDs per second, used for streaming long text.

**Medium was measured on the hardware** [test]. The test text was `>>START<< 01 … 45`, 144
characters (863 LEDs) plus the 72-LED panel width, so 935 LEDs per loop.

* Predicted from 17 ms per LED: 15.9 s per loop.
* Measured: **16.1 s (58.1 LEDs/s)**. The user read the time off a clock drawn on row 2 through a
  string variable, so the marquee was never restarted.

The two agree within about 1%, inside the reading's reaction-time error. This confirms that
the firmware's scroll table is milliseconds per LED, so slow (22 ms) and fast (12 ms) should
also be right. Updating a string variable once every 0.1 s while another row scrolls caused no
visible glitches.

## 10. Orientation

The display "can be mounted on either edge and the message will still display right-side-up
automatically ... by an accelerometer" [doc]. The whole frame is flipped, so text, LED coordinates
and scroll direction all stay relative to the viewer. Bits 0x0200 / 0x0400 lock it (section 4).

The fixed orientations are **relative to the hardware's factory orientation, not to the
viewer** [test]. This unit is mounted turned 180° from its factory orientation, and auto mode
corrects for that. The results with it mounted that way:

| Mode | Options word | Appearance |
|---|---|---|
| fixed "upside down" | 0x0E03 | read correctly |
| fixed "normal" | 0x0A03 | upside down |
| auto | 0x0803 | correct |
The help notes that scrolling text appears to "lean" differently when inverted, which is an
optical effect of the refresh.

## 11. Open questions

* Slow and fast marquee speeds weren't measured. Medium matched its table value within 1%.
* The CS 4/5 advance (11 or 12 LEDs). It doesn't affect fit decisions on a 72-LED-wide display.
* Option bit 0x0002 (set on this unit) and bit 0x0004 are undocumented.
* There is no known way to read the accelerometer's current flip state. Auto mode works
  without it.
* How often the DHCP retry ("Acquiring IP") happens. One dropout of about a minute (IP 0.0.0.0 in
  discovery) was seen during roughly an hour of testing. It no longer applies now that the unit
  has a static IP.
* Whether `<CLRWIN>` on a scrolling window always freezes the firmware. It happened once, and it
  wasn't repeated, to spare another power cycle.
