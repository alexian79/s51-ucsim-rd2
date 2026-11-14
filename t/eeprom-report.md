# Phase 7 — AT89C51ED2 on-chip data EEPROM behind `EECON` (0xD2)

Build tree: `/tmp/ucsim-eeprom` (full `cp -r` of `/home/oleksiy/s51/vendor/ucsim-stc`, then rsynced
to the current vendor state so the CKRL-prescaler/boot-ROM edits were present before I started).
Binary: `/tmp/ucsim-eeprom/ucsim/src/sims/s51.src/ucsim_51`.
Pristine pre-change snapshot of that build: `/tmp/ucsim-eeprom-base-bin` (used as the identity
reference and as the second control).
Suite: `/home/oleksiy/s51/t/eepromtest.py` (new, 30 legs). No doc, floor.sh, or existing suite was
touched. All page numbers below are **printed** pages of doc4235K (`/tmp/ds/pNNN.txt`, p000 = p.1),
with tables continued onto the following page read as well.

---

## 1. What the datasheet says

### 1.1 Which bits of `EECON` exist, and their polarity (table 23-1, printed pp.91-92; table 3-11, printed p.7)

| bit | name | polarity / behaviour |
|---|---|---|
| 7:2 | Reserved | "The value read from this bit is indeterminate. Do not set this bit." |
| 1 | `EEE` | Enable EEPROM **Space**: *set* ⇒ MOVX addresses are decoded into the EEPROM, *clear* ⇒ the XRAM space. Positive-asserted. |
| 0 | `EEBUSY` | Programming busy: "Set by hardware when programming is in progress", cleared by hardware at the end, **"Can not be set or cleared by software"**. |

Reset value `XXXX XX00b` (both defined bits reset clear, reserved bits indeterminate). The register
is **"Not bit addressable"** (printed p.92) — bit address 0D2h is `PSW.2` — so a guest cannot
`JB EEBUSY`; figures 23-1/23-2 (printed pp.90-91) poll it with `MOV A,EECON` + `JNB ACC.0`.

### 1.2 How the 2048 bytes are addressed (section 23.1, printed p.89)

**No dedicated address or data SFR, and no new address space.** Verbatim: "The 2K bytes on-chip
EEPROM memory block is located at addresses **0000h to 07FFh of the XRAM/ERAM memory space** and is
selected by setting control bits in the `EECON` register. A read or write access to the EEPROM
memory is done with a **MOVX** instruction". So `DPTR` carries the address and `ACC` the byte,
exactly as for external RAM, and the answer to "window in one of the address spaces" is: a
**per-access overlay of the low 2 KiB of the MOVX space while `EEE` is set**. That collides with the
on-chip expanded RAM, which section 9 (printed p.23) puts at the same addresses, sized by
`AUXR.XRS2:XRS0` (reset 010 = 768 B, table 9-1) and withdrawn by `AUXR.EXTRAM`; the datasheet never
relates the two mappings.

### 1.3 Erase vs write (section 23.1/23.2, printed pp.89-90)

There is **no erase trigger for the data EEPROM**. The whole write procedure is: check `EEBUSY`
clear → set `EEE` → `MOVX @DPTR,A` → clear `EEE` (figure 23-1); section 23.2 is its read mirror.
No page, no second write, no erase step. Contrast the Flash chapter, which *does* state a 128-byte
page and "It is not necessary to erase a byte or a page before programming" (section 24.2, printed
p.93). Erasing the data block exists only on the ISP side — "Program EEPROM data" (table 24-9,
printed p.110) — and via full chip erase (section 24.8.2, printed p.103, which "fills with FFh").

### 1.4 Security (section 24.8.1, printed p.103; table 24-7)

**Nothing in `EECON` locks the array** — there is no lock bit to model. The security the part has is
the Software Security Bits (levels 1/2), which gate *ISP* access to "Flash/EEPROM" (read-only at
level 1, none at level 2) and the Boot ROM API, whose usage constraints (section 24.10.2, printed
p.111) tell the application to disable interrupts around EEPROM-data calls. Neither is a bit the
running program can use to bar its own MOVX access.

### 1.5 Timing / endurance

Table 25-2 (printed p.115): `tWRITE  Flash or EEdata programming time  7 … 17 ms, 2.7 < VCC < 5.5V`.
The two-number rows of that table are Min and Max with Typ left blank (cf. the `VIL` row two lines
above), so **7 ms min / 17 ms max**. Features list (printed p.1): "On-chip 2048 Bytes EEPROM Block
for Data Storage (**AT89C51ED2 Only**) … **100K Write Cycles**". Section 23 gives no endurance or
retention figure; the "10 years data retention" of section 24.1 (printed p.93) is the Flash's.

### 1.6 Family vs ED2-only (the D14 question, verified from the pages)

"This feature is available only for the AT89C51ED2 device" (printed p.89); "(AT89C51ED2 Only)" in
the features list (printed p.1); "EEPROM data Memory SFR (AT89C51ED2 only)" (table 3-10, printed
p.7). Chapter 3 is a *family* chapter, so **`EECON` stays named on the RD2** — D14's
named-but-inert register — while the array behind it is ED2-only. Confirmed empirically: `info hw
eeprom` prints the element on `89C51ED2` only, and prints the usage line on `89C51RB2`, `89C51RC2`,
`89C51RD2`, `8052` and `stc12c5a60s2`.

---

## 2. Decisions (each taken deliberately, none silently)

**(a) Array persistence — RAM member, no backing file.** The array is a `unsigned char
array[2048]` in the element, filled with `0xff` once at construction and deliberately **not**
touched by `reset()`. `reset` is the only power-cycle-like event ucsim has, and content that
vanished under it would advertise the *opposite* of non-volatility; with this choice a guest can
write a byte, `reset`, and read it back — the one observable face of non-volatility a simulator has
(pinned by two legs). What `reset()` *does* clear is the volatile state (in-flight cycle, pending
byte/address, the four counters), as `cl_rd2_brg`/`cl_rd2_spi` do. **A backing file was rejected**:
ucsim has no notion of VCC being removed, so "when to load, when to flush" has no trigger inside the
model, and a file a stray `-t 89C51ED2` run rewrote would put stale bytes in front of every suite in
the floor. If it is ever wanted, the honest form is an explicit **default-off** option that names
the file (nothing loads or flushes unless named) — that is the activation rule, stated here so the
option cannot be added as a default-on side effect.

**(b) `EEBUSY` and `tWRITE` are modelled in simulated time, at the worst case.** The boot-ROM slice
declared `tWRITE = 0 cycles` for IAP; this slice does not borrow that call. The window is
`uc->clocks_of_time(0.017)` **oscillator clocks** — 204000 at 12 MHz — and `tick()` accumulates
`cycles * uc->clock_per_cycle()`, i.e. the same clock count `state` reports. Time, not cycles,
because programming is an on-chip charge pump given in milliseconds: `CKCON0.X2` and the CKRL
prescaler both change what a cycle is worth (section 6.2.1, printed p.16) and nothing about a
millisecond — the suite measures the window at 203988 clocks in **both** standard and X2 mode, which
is what proves it is time. 17 ms (Max) rather than 7 (Min) because a guest whose wait loop passes
here cannot fail on this axis on silicon: the only direction a simulator may be wrong.
A guest observes it exactly as the datasheet tells it to: `EEBUSY` reads 1 from the MOVX that starts
the cycle until hardware clears it, and because section 23.1 says the segment "is not available for
reading or writing" meanwhile, the byte **commits at completion** (not at issue), a read anywhere in
the window while busy answers `0xff` and counts as a blocked read, and a write during the window is
**refused and counted, not queued** (one byte in flight; a single-port array cannot hold two).
`EEBUSY` is raised/cleared *in the register cell* (`cl_memory_cell::write`), so both the program and
the console see the same byte.

**(c) A plain RD2 writing `EECON` behaves exactly as before.** Accepted and inert: no element is
constructed at all on the RD2, so nothing consults `EEE` and `EEBUSY` never answers 1.
`t/xramtest.py::eecon_does_not_remap` is untouched and green, `t/ident89c51r.py` is byte-identical
against the pre-change build, and `eepromtest.py` re-runs the negative control on the RD2 in three
legs.

**(d) The gating mechanism is the device *name*, not a cpu flag.** `rd2_is_ed2(struct cpu_entry*)`
(static in `uc89c51r.cc`) tests `type & CPU_89C51RD2` **and** `strcmp(ent->type_str, "89C51ED2")`.
The flag route is not available: `cl_sim51::mk_controller` (sim51.cc:91) switches on the *exact*
enum value of the device-table row, so a combined `CPU_89C51RD2|CPU_89C51ED2` row falls through to
"Unknown processor type" unless `sim51.cc` and `globals.cc` are widened, while a *separate* ED2 bit
would strip the ED2 of every `type & CPU_89C51RD2` gate — the XRAM window, X2, CKRL, SPI, keyboard,
BRG, boot ROM and its SFR names, all of which the ED2 has. `type_str` is the table's own spelling
(`mk_controller` has already `strcasecmp`d `-t` and hands over `&cpus_51[i]`), so `strcmp` is exact.
Registration sits in `cl_uc89c51rd2::mk_hw_elements()` next to the other RD2 elements; since
`add_hw()` runs `added_to_uc()`, the class needs no predicate of its own.

**(e) `EEE` wins over `AUXR.EXTRAM`, and the edge is 07FFh.** Section 23 states `EEE`
unconditionally while the on-chip XRAM is the conditional mapping, so with `EEE` set the array is
answered for 0000h..07FFh regardless of `AUXR`, and 0800h and above stay whatever `AUXR` says.
Mechanism: the element registers itself as a **memory operator on xram cells 0..0x7FF** and decides
*per access* — with `EEE` clear it returns the cell untouched, so `decode_xram()`'s
banker-selected mapping (and the whole external bus) is exactly what a program sees. A decoder or a
second `cl_banker` was rejected: the seven per-page bankers already own 0000h..06FFh, decoders in one
space are mutually exclusive rather than stackable, and the mapping under `EEE` is a function of
`AUXR`, not a fixed chip to bank *to*. Both `AUXR` states are pinned by legs.

**(f) `cl_hw` id 17 (`"eeprom"`), claimed against the headers, not against upstream.** Checked
2026-11-14 against `rd2_brgcl.h:30` (`RD2_BRG_ID` 13), `rd2_kbdcl.h:47` (`RD2_KBD_ID` 14),
`rd2_spiccl.h:50` (`RD2_SPI_ID` 15), `rd2_bootcl.h:63` (`RD2_BOOT_ID` 16 — the boot-ROM work has
landed, so 16 is taken), `serial.cc:124-131` (dispatches on 2 and `RD2_BRG_ID`), `stc12_brt.cc` (3),
`stc12_adc.cc` (10), `stc12_wdt.cc` (11), `stc15_timer2.cc` (12) and `timer0.cc`/`timer2.cc` (0, 1,
2). 13/14/15/16 are unchanged by this slice. The reason the id is claimed in the header comment
beside those file:line references is that `info hw` resolves elements by **name**, so two elements
sharing an id are invisible to every suite (the SPI and the keyboard both answered 14 once). Nothing
partners with it: it publishes no events.

**(g) Counters key on `bytes=` / `*_events=`** (`bytes=… program_events=… blocked_reads=…
blocked_writes=…`) so no counter label can collide with a flag name in `info hw` output.

**(h) Erased state is a declared convention.** doc4235 never states the shipped contents of the data
block, so the model starts it at `0xff` — the level an EEPROM cell rests at and the value the full
chip erase "fills with FFh" (section 24.8.2, printed p.103). Pinned by a leg so the convention cannot
drift silently.

**(i) Wear is not modelled.** 100K cycles (printed p.1) has no observable consequence in a
simulation and no counter would be checked against it.

---

## 3. Diff surface

`diff -rq` of the vendor tree vs `/tmp/ucsim-eeprom`, excluding `*.o`, `Makefile.dep`, `ucsim_51`,
`__pycache__` — **only these five files, exactly as intended**:

```
Files .../s51.src/objs.mk            differ   (+1 line: rd2_eeprom.o on the rd2_brg.o rd2_kbd.o
                                              rd2_spi.o rd2_boot.o line)
Only in /tmp/.../s51.src: rd2_eeprom.cc      (new, 262 lines)
Only in /tmp/.../s51.src: rd2_eepromcl.h     (new, 155 lines)
Files .../s51.src/regs51.h           differ   (+bmEEE 0x02, +bmEEBUSY 0x01, EECON comment extended:
                                              bit-addressability warning + which suite pins what)
Files .../s51.src/uc89c51r.cc        differ   (+#include "rd2_eepromcl.h", +static rd2_is_ed2(),
                                              +gated add_hw in cl_uc89c51rd2::mk_hw_elements(),
                                              site comment rewritten to name the fifth element)
```

`objs.mk` is the only build file that names objects here (`Makefile.dep` is generated and excluded);
nothing else needed touching. `RD2_SPI_ID`/`RD2_KBD_ID`/`RD2_BRG_ID` are untouched, and `serial.cc`,
`uc89c51rcl.h`, `globals.cc` and `sim51.cc` are **not** in the diff.

New file: `/home/oleksiy/s51/t/eepromtest.py` (30 legs, `S51_SIM`/`S51_TIMEOUT`/`S51_REF`/
`EEPROMTEST_FORCE`; not added to `floor.sh`, which I did not edit).

---

## 4. Floor before / after

| | before (`/tmp/floor-before2.txt`) | after (`/tmp/floor-after.txt`) |
|---|---|---|
| command | `S51_TIMEOUT=90 t/floor.sh <vendor build>` | `S51_TIMEOUT=90 t/floor.sh /tmp/ucsim-eeprom/.../ucsim_51` |
| result | `floor: 0 suite(s) not green`, `FLOOR-EXIT=0` | `floor: 0 suite(s) not green`, floor exit 0 |
| `runtests` | 11/20 (documented baseline) | 11/20 (unchanged) |

After-run suite lines: `ident89c51r 36/36`, `rd2identity 38/38`, `rd2registers 288/288`, `banktest
3/3`, `ccapmrst 12/12`, `dpstest 2/2`, `t2oetest 7/7`, `wdttest 14/14`, `x2test 15/15`, **`xramtest
10/10 … EECON remap leg OK`**, `irctest 50/50`, `pcapin 21/21`, `euarttest` all passed, `uartmp 19
checks 0 failures`, `runtests 11/20`, `kbdtest 30`, `spitest` all passed, `ckrltest` all passed,
`bootromtest` all passed.

Leak guard, run explicitly against the pre-change build:
`t/ident89c51r.py /tmp/ucsim-eeprom/.../ucsim_51 /tmp/ucsim-eeprom-base-bin` → **PASS: 36/36
transcripts byte-identical**, so the RD2 gained nothing observable. `eepromtest.py` with
`S51_REF=/tmp/ucsim-eeprom-base-bin` adds a byte-identity leg of its own (3 legs, 0 diffs) →
**PASS: 30/30** on my binary.

---

## 5. Control audit (legs that pass on a binary without the feature)

`EEPROMTEST_FORCE=1` skips the fail-fast guard (which otherwise exits 1 with the named reason
"`<binary>` has no `eeprom` element for `-t 89C51ED2` … every leg below would be measuring its
absence"). Two controls were run; transcripts kept in `/tmp/eeprom-control-refb.txt` and
`/tmp/eeprom-control-base.txt`.

**`/tmp/s51-ref-b` (pre-Phase-1: no keyboard, no SPI, no BRG, no EUART, no boot ROM) — 5 of 30
PASS**, exit 1:

| leg | why it legitimately passes |
|---|---|
| `RD2: NO eeprom element (D14 negative control)` | an absence guard; it *must* pass everywhere |
| `RD2 and ED2: the whole D0h SFR page dumps identically` | an equality guard; on a binary with no model the two names are trivially equal (weaker there than here — see the base control) |
| `ED2: 0x0800 with EEE set stays on the bus (readable back, no aliasing)` | upper-bound absence guard: the bus is the *default* mapping, so every binary answers it; its job is to catch a model that maps past 07FFh |
| `RD2: EEE set leaves the MOVX target alone (0x0100 reads 0x22 both times)` | RD2 negative control |
| `RD2: EEBUSY stays 0 after MOV EECON,#0x02` | RD2 negative control |

**`/tmp/ucsim-eeprom-base-bin` (current vendor minus my change — the confound-free control, since it
*does* have the keyboard/SPI/BRG/boot ROM and the named-inert `EECON`) — 9 of 30 PASS**, exit 1. The
five above, plus four register-level legs that describe D14's named-but-inert state and therefore
*must* pass both before and after my change: `EECON is named and resets to 0x00`, `EECON reads 0x00
again after reset`, `RD2: its EECON row is the D14 one`, `ED2: its volatile state does not survive
(EECON reads 0x00 after reset)`.

**Every behaviour-bearing leg fails on both controls**, including the ones that initially passed
vacuously and were rewritten for it (measured on ref-b in brackets): array round-trip [it read back
`3c c3` — a plain XRAM write-through also does that], so it now asserts four bytes at once, the array
answering `3c c3` at the two addresses whose XRAM bytes must still read `a5 5a` [`3c c3 3c c3` →
FAIL]; the array-vs-cell leg [read back `0x11`] → one case asserting cell `0x11` *and* MOVX read
`0x3c`; the 07FFh boundary [`0x77`/`0x77`] → one case asserting `0x77` with `EEE` set and **not**
`0x77` with it clear; `EEBUSY clears` [`0x02`] → asserts the transition `0x03 → 0x02`; "the byte
appears once the flag clears" [`0xaa`] → asserts `0xff → 0xaa`; persistence [`0x77`] → the console
pokes `0x11` into the plain cell after `reset`, so only an array that outlived the reset answers
`0x77` [`0x11` → FAIL]; the security leg [`0x66`] → one case asserting array `0x66` **and** the byte
under it `0x11`; both `tWRITE` legs [0 clocks]; the counters leg [`?? ??`].

---

## 6. Console traps hit in this slice (new ones)

* **`set memory xram <a> <v>` with `EEE` set is NOT a backdoor.** It goes through the same memory
  operator and *starts a programming cycle* (measured: `pending` became `0x0100:0x11`, `busy=1`, and
  the window then read `0xff`). The notebook's earlier note that "`set()` bypasses hooks" held for a
  different mechanism and is wrong here; the array-vs-cell leg now pokes the cell at a breakpoint
  where the image has already cleared `EEE`.
* **Any console peek at `@DPTR` or the SFRs consumes a blocked read.** `state`/register display reads
  the window through `read()`, so `blocked_reads` was 4 after a session whose program never read
  during the window. Assert counters only in a session whose console stayed out of the window.
* `dump xram` **does** follow the remap (it goes through `read()`), so with `EEE` set it shows the
  array — useful, but it means the plain map under the window cannot be inspected while `EEE` is set.
* `-X` sets the crystal (`-X 12M`, `-X 12000000`, `-X 12000k` all parse); there is no `-f`. Pinning
  12 MHz makes `tWRITE` an exact 204000 clocks.
* **A `dump iram` must be issued explicitly** — `run_to_end` now emits `dump iram 0x40 0x4f` after
  `go`; four legs were silently reading `??` before that, and one leg needed two images in one session
  because the array lives in the process, not in the hex file.
* **The assembler's label table must be keyed per wait block.** Two `wait_eebusy()` copies in one
  image shared label names, so the first block's `JNB` resolved to the *second* block's tail and
  silently swallowed the write in between (the second programming cycle never started; `bytes=1`).
  Only a byte-exact `fprintf` trace through the write hook exposed it. `wait_eebusy()` now mints a
  unique tag per call and says so in its comment.
* Hand-assembled `MOV Rn,#data` / `DJNZ Rn,rel` are `0x78+n` / `0xD8+n`: using `0x7D`/`0x07`/`0xD7`
  produced a "wait loop" that decremented `R7`/`R5` through garbage and fell out immediately — which
  looked exactly like `EEBUSY` never being set, and cost a detour through the boot ROM hypothesis.
* Long scripted `step 1 ; dump ; …` runs are still unreliable; a rate measurement needs one `step`/
  `go` and one query, and `clocks()` differences (two images differing in one byte) were used, never
  an absolute elapsed time.

---

## 7. Reproducing

```sh
make -C /tmp/ucsim-eeprom/ucsim/src/sims/s51.src -j8
S51_SIM=/tmp/ucsim-eeprom/ucsim/src/sims/s51.src/ucsim_51 \
S51_REF=/tmp/ucsim-eeprom-base-bin S51_TIMEOUT=120 python3 /home/oleksiy/s51/t/eepromtest.py   # 30/30
S51_TIMEOUT=90 /home/oleksiy/s51/t/floor.sh /tmp/ucsim-eeprom/ucsim/src/sims/s51.src/ucsim_51   # 0 not green
S51_TIMEOUT=90 python3 /home/oleksiy/s51/t/ident89c51r.py \
  /tmp/ucsim-eeprom/ucsim/src/sims/s51.src/ucsim_51 /tmp/ucsim-eeprom-base-bin                  # 36/36
EEPROMTEST_FORCE=1 S51_SIM=/tmp/ucsim-eeprom-base-bin S51_TIMEOUT=60 \
  python3 /home/oleksiy/s51/t/eepromtest.py                                                     # 9/30, exit 1
```
