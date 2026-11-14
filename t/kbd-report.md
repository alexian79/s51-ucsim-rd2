# AT89C51RD2 keyboard interface — worker report (agent `KeyboardPhase3`)

Tree: `/tmp/ucsim-kbd` (private copy of `~/s51/vendor/ucsim-stc`; the vendor tree was
never written to, and `git status` there is unchanged by me). Suites:
`/home/oleksiy/s51/t/kbdtest.py` (new) and the block appended to
`/home/oleksiy/s51/t/irctest.py`. Controls: `/tmp/s51-ref-b` (pre-fix reference) and the
vendor build `~/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51` — Main's current
tree, which has the `KBLS`/`KBE`/`KBF` name rows but no keyboard, and is the honest
cutover control.

## 0. Status

| item | result |
|---|---|
| build | clean, no warnings |
| binary validated | `/tmp/ucsim-kbd/ucsim/src/sims/s51.src/ucsim_51` |
| build command | `make -j8 -C /tmp/ucsim-kbd/ucsim/src all` |
| `t/kbdtest.py`, my binary | **30/30 PASS** (`Checks run: 30`) |
| `t/kbdtest.py`, vendor build | **1 PASS / 29 FAIL**, exit 1, ~1 s, no hangs |
| `t/kbdtest.py`, `/tmp/s51-ref-b` | **1 PASS / 29 FAIL**, exit 1, ~1 s, no hangs |
| `t/irctest.py`, my binary | **50/50 PASS** (was 37 checks; 13 appended, no existing leg modified) |
| `t/irctest.py`, vendor build | **42/50** — the 8 failures are 8 of the 13 new legs (§8) |
| `t/irctest.py`, `/tmp/s51-ref-b` | **31/50** (the pre-existing level-fix legs fail there as well; that is that binary's known state, not my block) |
| `t/rd2registers.py`, my binary | **284/284 PASS** (targeted: the suite most likely to break from a new SFR cell registration; it did not) |
| strip ablation | reverting `uc51.cc:2223-2224` gives **48/50**, failing exactly the two strip legs (§8) |
| floor | not run per instruction — `Main` runs it |
| `t/kbdtest.py` state | **complete as it stands** (30 checks; every figure in this report is asserted by a leg) |

**Every check that passes on a keyboard-less binary, and why that is legitimate.** In
`kbdtest.py` there is exactly one: A6, whose claim *is* the absence of the interface on
the AT89C51RB2/RC2, so passing on both controls is the point. A1 used to be a second one
— "read 0x9c/0x9d/0x9e and expect 0" passes anywhere because unwritten SFR space reads 0
— and it has been replaced by something only the model can answer (§7). In `irctest.py`
five of the thirteen new legs pass on the vendor build: `KBD vs t1 both L0`,
`KBD L1 vs t1 L1`, `KBD L0 vs t1 L1`, `KBD pair1 bit7 only: tie` and
`89C51R: no keyboard arbitration`. All five expect **timer 1** to keep the vector, and
with no keyboard registered timer 1 always keeps it. They are the negative half of the
ladder — they are what proves a levelled keyboard does not steal a vector from an equal
or higher timer — and they are also what exposed the setup-overruns-its-own-vector bug in
§8, where every one of them "passed" for the wrong reason at once. The remaining eight
new legs fail on the vendor build, each printing the value it measured.

The 29 `kbdtest` control failures fall into three named classes (per-leg table in §7):
`NO kbd[] ELEMENT (element absent)` (A1, A2, A3, D1-D3, E10-E13, E2 — no `cl_rd2_kbd`, so
`info hardware kbd` answers `info hardware category`); `NO ROW AT 0x00003b` (A4, A5, C9);
and the firmware deadline being reached instead of the vector (B1-B5, C1-C8, C10). On
`/tmp/s51-ref-b`, `dump sfr 0x9c 0x9e` additionally prints an unnamed row
(`0x9c  00 00 00`) because that binary predates the KBLS/KBE/KBF name rows.

### The two checks that were failing when Main last looked, and what the fix was

`C8` and `C9`. **There was no model defect and no `sticky request_` fix** — that was my
mid-session hypothesis and reading the code disproved it: `request()` computes
`(keys & KBE) != 0` live (`rd2_kbd.cc:123-129`), `enabled()` reads `IEN1` live
(`:131-135`), and `EA` is tested by `do_interrupt()` itself (`uc51.cc:2110`), so nothing
is cached anywhere. The failure was in the harness: `dump sfr 0x9c 0x9e` is not a
passive read once `0x9e` carries a read-clear hook — running it *before*
`info hardware kbd` consumed the flag and the instrument then correctly reported
`request=no` with a key held down. `run_case()` now asks the instrument first and the
dumps second, and both legs pass. This is written into the harness comment at
`t/kbdtest.py:357-363` because the transcript is indistinguishable from a masking
defect.

Two other harness bugs found the same way, both of which had produced plausible wrong
numbers earlier: IRAM is not zeroed by `reset` in this build (an entry counter read
"232 entries" for a handler that ran once — every counter is now written by the setup),
and a `dump iram` parser that stopped at the first row answered `None` for a second,
non-overlapping row.

## 1. Files created

| file | lines | what it is |
|---|---|---|
| `/tmp/ucsim-kbd/ucsim/src/sims/s51.src/rd2_kbdcl.h` | 108 | class declarations + the semantics argument + the register/interrupt cites |
| `/tmp/ucsim-kbd/ucsim/src/sims/s51.src/rd2_kbd.cc` | 259 | the model: `cl_rd2_kbd` (comparator, KBF hooks) + `cl_rd2_kbd_it` (the source) |

`cl_rd2_kbd` is a `cl_hw` in category `HW_TIMER` with id `RD2_KBD_ID = 14` (the next free
`HW_TIMER` id after the BRG's 13; the category is reused exactly as `cl_stc12_adc`
reuses it, and nothing may partner with it).

## 2. Files edited (four hunks, all in the private copy)

| file | hunk |
|---|---|
| `regs51.h:101-103` | `#define IP_PAIR1 0x80`, `bmKBD 0x01`, `bmKBDL 0x01` with cites — **see §8, this is the one placement question Main reserved** |
| `uc51.cc` | deleted the TU-local `#define IP_PAIR1 0x80` (was ~2179); the pair-1 arm of `priority_of()` now strips the selector: `l= sfr->get(IPL1) & (nuof_it & ~IP_PAIR1)`, same for `h` (`uc51.cc:2216-2225`); two comment blocks rewritten (`:2168-2198`) |
| `uc89c51r.cc:38, 359` | `#include "rd2_kbdcl.h"`, and `cl_uc89c51rd2::mk_hw_elements()` does `add_hw(h= new cl_rd2_kbd(this)); h->init();` after the BRG |
| `objs.mk:1, 11` | `rd2_kbd.o` appended to `OBJECTS`, date/comment line |

**The `priority_of()` strip is functional, not cosmetic.** The pair-1 arm ANDed the
whole `ie_mask` with `IPL1`/`IPH1`, and `IP_PAIR1` (0x80) collides with bit 7 of both
registers, which are Reserved and read back what was written (tables 17-7/17-8,
pp.78-79). Without the strip, `MOV IPL1,#0x80` hands every pair-1 source level 1. Leg
E2 pins this: `IPL1=IPH1=0x80` must leave the keyboard at level 0.

`it_priority()` in `uc89c51r.cc` is dead code (nothing calls it) and was not touched.

## 3. Semantics as implemented, with the page text behind each

Printed page *N* is `/tmp/ds/p(N-1)NN.txt`. All quotes below were read from the page
files, not from `doc4235-spec.md`.

* **What the block is** (p.59, `p058.txt`): "8 inputs with programmable interrupt
  capability on both high or low level ... available as alternate function of P1 and
  allow to exit from idle and power-down modes"; "The keyboard inputs are considered as
  8 independent interrupt sources sharing the same interrupt vector"; global enable
  "KBD in IE1" (`IE1` is the prose name for `IEN1` 0xB1 bit 0, table 17-6 p.77);
  "Level detection is then reported in interrupt flags KBF.x that can be masked by
  software using KBE.x"; figure 15-2 draws the request as `KBF.x AND KBE.x` ORed into
  `KBDIT`. Implemented: `request()` = `keys & KBE` (`rd2_kbd.cc:123-129`),
  `global_enabled()` = `IEN1 & bmKBD` (`:131-135`), one source at one vector.
* **`KBF` 0x9e** (p.60, `p059.txt`, table 15-1): reset `0000 0000b`; "read only access,
  all flags are automatically cleared by reading the register"; per bit "Set by hardware
  when the Port line N detects a programmed level". Read-clear is implemented on the
  **read path of the 0x9e cell** (`rd2_kbd.cc:182-193`: return the old value, zero
  `keys`, `cell_kbf->set(0)`), and `write()` replaces the incoming value with the
  model's own (`:166-171`) so `MOV KBF,#0xff` with no key reads back `0x00` (leg D3).
  Legs: D1 (released key: three reads give `01 00 00` — the flag survives one read and
  that read clears it), B5 (the handler's read clears it), D3 (read-only).
  The per-bit rows also name a `KBIE` register that **does not exist** anywhere in the
  document's register map (it is `KBE`); that is a datasheet bug and nothing named
  `KBIE` was implemented.
* **`KBE` 0x9d** (p.61, `p060.txt`, table 15-2): reset `0000 0000b`; "Cleared to enable
  standard I/O pin. Set to enable KBF.x bit in KBF register to generate an interrupt
  request." So `KBE` gates the **request**, never the flag, and never the pin. The
  flag/mask separation is asserted directly: **C1** (`KBE.0`=0, P1.0 low → `KBF`=0x01
  with `request=no`), **C5** (`KBE`=0x00 with all eight lines programmed high →
  `KBF`=0xff, `request=no`), **C7** (per-bit: `KBF.0` set, `KBE.0` clear → no request),
  and **C8/C9** (with the request live, `En`=en and `Req`=YES in
  `info hardware irq` while `EA`=0 keeps the vector from being taken).
* **`KBLS` 0x9c** (p.62, `p061.txt`, table 15-3): 0 = a low level is a key, 1 = a high
  level. Both senses are evaluated for all eight lines each sample
  (`rd2_kbd.cc:107-121`, `hit = (~pins & ~kbls) | (pins & kbls)`). Polarity is pinned in
  **both directions on the same pin** (P1.0, driven low): **C1** flags it with
  `KBLS.0`=0, **C1b** does not flag it with `KBLS.0`=1. The complementary pair on the
  high side is **C4** (`KBLS`=0 with every line high → no flag) and **C5** (`KBLS`=0xff
  with every line high → all eight flagged).
* **P1 is read, never written — the block is a pure consumer.** `cell_p1` is only ever
  read, and only through `cell_p1->read()` (`rd2_kbd.cc:114`), i.e. through
  `cl_port::read()`, which answers `latch AND port_pins` — the same value `MOV A,P1`
  answers, so a firmware `MOV P1,#..` presses and releases keys with no console
  plumbing. There is **no write hook on P1 and no `set()` on that cell anywhere in the
  file** (grep: `cell_p1` appears at `:21`, `:45`, `:112`, `:114`, `:215` — declaration,
  resolution, two null-guards, a read, and the instrument line). Consequence for the
  SPI block that follows: P1.5/6/7 (MISO/SCK/MOSI) and the PCA's CEXn pins stay owned by
  their own blocks; the keyboard can only observe the pins. It also means a
  keyboard-active pin and an SPI pin can be modelled at the same time without either
  block fighting over the latch.
* **Sampling** (pp.18-19, `p017.txt`/`p018.txt`, table 7-1): `CKCON0` lists
  `WDX2 PCAX2 SIX2 T2X2 T1X2 T0X2 X2` and no keyboard bit (`CKCON1` likewise), so the
  detector is asynchronous and is sampled once per machine cycle from `tick()`
  (`rd2_kbd.cc:149-155`) rather than counted. `do_inst()` runs `tick_hw()` before
  `do_interrupt()`, so a `MOV P1,#..` that presses a key is visible at the same
  instruction boundary. Main confirmed the CKCON reading independently.
* **Why not the `EV_PORT_CHANGED` hook the brief suggested** (`rd2_kbd.cc:137-147`): the
  comparator's answer also changes when `KBLS` is written and when the part comes out of
  reset with `P1` at `0xff` and a `KBLS` bit set, and a port-change event reports
  neither. Leg **C5** is exactly that second case: `KBLS`=0xff alone, with no pin
  activity, flags all eight lines.

## 4. Level semantics, i.e. what the datasheet does not say (Main's item 5)

Nothing in section 15 states whether `KBF.x` is set only on the transition into the
programmed level or continuously while the level is present. **Choice: level** — the
flag is set for as long as the programmed level is applied, so after software clears it
(read) **the request re-asserts on the next sample while the key is still down**; it
does not go silent until a new event. Reason: p.82 (`p081.txt`) lists the keyboard with
INT0#/INT1# as a power-down exit with "same behavior as external interrupt", and the
external interrupts are level-sampled; section 15's own wording is "detects a programmed
level", not "on the transition". Asserted by **D2**: with the key held, two consecutive
reads of `KBF` both return `0x01`; contrast **D1**, where the level is gone and the
second read returns `0x00`. A pulse/edge reading would make D2 `[0x01, 0x00]`.

Practical consequence for test firmware, and the reason every setup here reads `KBF`
once before enabling anything: because `P1` resets to `0xff`, programming `KBLS` with a
high-detect bit **is** a key out of reset (C5). Setups therefore do
`MOV A,KBF` → `MOV 0x3f,A` after the register writes and before `IEN1`/`IE`, which
clears anything the setup itself caused, and the "must not fire" legs assert `KBF`==0
after that clear. Releasing means driving the line to the level `KBLS` says is *not* a
key, so a high-detect line is released **low** — `kbd_isr(release_to=0xfe)`; using
`0xff` there makes the handler press the key again and re-enter forever.

## 5. Interrupt wiring

* vector `0x003b`, polling rank 8 → `cl_rd2_kbd_it` passes `addr=0x003b`,
  `poll_priority=8` (`rd2_kbd.cc:234-244`). `it_sources` is a `cl_sorted_list` keyed on
  `poll_priority` so `add()` places rank 8 behind timer #2 by itself; A5 asserts the
  order (`0x00002b` then `0x00003b`) and A4 asserts the row exists and is named
  `keyboard`.
* enable: `IEN1` (0xB1) bit 0 = `KBD` (table 17-6, p.77). `enabled()` is overridden to
  answer from `IEN1 & bmKBD` alone, because the `ie_mask` also carries the `IP_PAIR1`
  flag and the base `enabled()` ANDs that mask with the enable cell — a program that
  wrote `0x81` to `IEN1` would then find the keyboard disabled (bit 7 is Reserved).
* levels: `IPL1`/`IPH1` (0xB2/0xB3) bit 0 = `KBDL`/`KBDH` (tables 17-7/17-8,
  pp.78-79) — the second priority pair, so the source declares `IP_PAIR1` in its
  `ie_mask` and reaches the pair-1 arm of `priority_of()`. E10-E13 pin all four levels
  (0/1/2/3 for `L`/`H` combinations, `L`=+1, `H`=+2); E2 pins that the Reserved bit 7 of
  `IPL1`/`IPH1` cannot raise the level. **Because the pair-1 arm previously ANDed the
  whole mask (including `IP_PAIR1`) with the registers, the strip described in §2 is a
  prerequisite for these legs and the SPI block will inherit it.**
* `clr_bit=false`: the flag is not owned by the interrupt system, it is owned by the
  read of `KBF` (p.60). The engine's `winner->clear()` is therefore a no-op for this
  source, which is why B2 can assert `KBF`=0x01 at the moment the vector is taken.
* gating is `CPU_89C51RD2` alone; **A6** asserts `89C51R` gets neither the element nor
  the source, so the AT89C51RB2/RC2 transcripts stay byte-identical.

## 6. How a test drives a keyboard transition

No cfg variable, no console command, no new plumbing: a key is a `MOV P1,#imm` in the
test image, because the comparator reads the pins through the port's own read hook
(`latch AND port_pins`). `press` = write the level `KBLS` calls a key; `release` = write
the other level (the handler does it, so the vector can be taken exactly once). Timing:
`do_inst()` ticks the hardware before `do_interrupt()`, so the request exists at the
same instruction boundary as the write; the global enable is written **last** in every
setup so no vector can be taken before the marker port is cleared.

Every image ends with a firmware deadline (`MOV R7,#0xff` / `DJNZ R7,$` / `SJMP $`) and
**both** the vector and that deadline are breakpoints, because `break` + `go` on an
unreached breakpoint wedges the console. That is what makes every leg fail by name (in
1.06 s total for 30 legs) on a binary that has no keyboard at all. Other console
reality encoded in the harness: there is no `set sfr`/`set var` in this build;
`dump data` is not a command (`dump iram lo hi` is); `dump sfr` needs a range;
port *bit* reads answer 0 so the latch is read instead; `info hardware irq` prints 6
hex digits where `dump` prints 8, so the row matcher matches the value, not a width;
IRAM is not zeroed by reset.

## 7. `kbdtest.py` legs (30 checks). "control" = vendor build **and** `/tmp/s51-ref-b`,
which agree on every row below unless stated.

| leg | claim | mine | control, named reason |
|---|---|---|---|
| A1 | the element watches the **real `P1` cell**: instrument reports `P1=0xff` at reset and `P1=0xfe` after `MOV P1,#0xfe`, `dump sfr 0x90` agrees, and that press flags `KBF=0x01` | PASS | FAIL — `NO kbd[] ELEMENT`; replaces the old "reset reads 0" leg, which was vacuous (unwritten SFR space reads 0) and passed on the vendor build |
| A2 | `kbd` element exists, reports the three registers idle, `request=no KBD=0 EA=0` | PASS | FAIL — `NO kbd[] ELEMENT (element absent)` |
| A3 | second instrument line: `vector=0x003b poll=8 level=0` | PASS | FAIL — no element |
| A4 | a source at `0x00003b` named `keyboard` | PASS | FAIL — `NO ROW AT 0x00003b` |
| A5 | `KBD` polls behind timer #2 (table 17-2 rank 8) | PASS | FAIL — no row |
| A6 | absent on `89C51R`: no element, no source | PASS | **PASS (must pass on both — guard)** |
| B1 | entry PC is `0x003b` | PASS | FAIL — deadline `0x26` reached, nothing vectors |
| B2 | `KBF`=0x01 as the vector is taken, handler body not yet run (`P4`=0) | PASS | FAIL — no vector |
| B3 | handler at `0x003b` ran and read `KBF` as 0x01 | PASS | FAIL — `P4`=0x00 |
| B4 | exactly one entry after release (counter written by setup) | PASS | FAIL — counter 0x00 |
| B5 | the handler's read cleared `KBF`, level gone → `request=no` | PASS | FAIL |
| C1 | `KBE.0`=0: `KBF.0` still set by the low pin, `request=no`, no vector | PASS | FAIL — deadline |
| C1b | same low pin with `KBLS.0`=1: no flag | PASS | FAIL |
| C2 | `IEN1` bit 0 clear: flag set, `KBD`=0, no vector | PASS | FAIL |
| C3 | `EA` clear masks it like every other source | PASS | FAIL |
| C4 | `KBLS`=0 with every line high is not a key | PASS | FAIL |
| C5 | `KBLS`=0xff with every line high flags all 8 (flag set with `KBE`=0) | PASS | FAIL |
| C6 | one high-detect line: only bit0 flags, only bit0 fires, one entry | PASS | FAIL |
| C7 | per-bit `KBE` mask: `KBF.0` set, `KBE.0` clear → no request | PASS | FAIL |
| C8 | request live with `KBD`=1 while `EA`=0 → still no vector | PASS | FAIL |
| C9 | irq row `En`=en and `Req`=YES while `EA` masks the vector | PASS | FAIL — `NO ROW AT 0x00003b` |
| C10 | a request that exists before `PCON.idl` ends idle (p.59, p.82) | PASS | FAIL |
| D1 | key released: reads give `01 00 00` (survives one read, that read clears) | PASS | FAIL — no flag to latch |
| D2 | key held: two reads give `01 01` (level reading, §4) | PASS | FAIL |
| D3 | `KBF` read-only: `MOV KBF,#0xff` with no key reads back 0x00 | PASS | FAIL — a plain cell answers `0xff` |
| E10-E13 | `IPL1`/`IPH1` bit 0 → level 0/1/2/3 | PASS ×4 | FAIL ×4 — no element |
| E2 | Reserved `IPL1`/`IPH1` bit 7 does not raise the level | PASS | FAIL |

## 8. Arbitration legs appended to `t/irctest.py` (37 → 50 checks)

One contiguous block inserted before the results printer; no existing leg was touched.
`irctest.py` count: **37 before, 50 after**, 50/50 on my binary.

The block arbitrates the keyboard against **timer 1**, a source on the *first* pair
(`PT1`/`PT1H`, `IP`/`IPH` bit 3), because the keyboard takes its level from the *second*
pair (`IPL1`/`IPH1` bit 0) and its enable from `IEN1` bit 0 — a first-pair source is the
only thing that can exercise the pair-1 arm of `priority_of()`. Marker port is **P2**
(P1 is the keyboard's input, so marking there would change which source is requesting;
`dump sfr[0xc0]` has no name on the RB2/RC2, which rules P4 out for the cross-chip leg).
Ports reset to `0xff`, so "nothing vectored" is `0xff`, timer 1 is `0x1b`, keyboard is
`0x3b`. Both requests are raised while `EA` is clear and `EA` is written last, so they
compete at one instant rather than one arriving first.

| new leg | expect | mine | vendor build |
|---|---|---|---|
| `KBD pair: rows at 0x3b` | 1 | PASS | FAIL — measured 0 |
| `KBD pair: one source added` | `len(89C51R)+1` = 15 | PASS | FAIL — measured 14 |
| `KBD pair: polls last` | `keyboard` | PASS | FAIL — measured `timer #2 TF2` |
| `KBD vs t1 both L0` | `0x1b` | PASS | PASS (no keyboard to take it — see §0) |
| `KBD L1 vs t1 L0` | `0x3b` | PASS | FAIL — measured `0x1b` |
| `KBD L1 vs t1 L1` | `0x1b` | PASS | PASS (tie, timer 1 by poll order) |
| `KBD L2 vs t1 L1` | `0x3b` | PASS | FAIL — measured `0x1b` |
| `KBD L3 vs t1 L2` | `0x3b` | PASS | FAIL — measured `0x1b` |
| `KBD L0 vs t1 L1` | `0x1b` | PASS | PASS (timer 1 keeps it either way) |
| `KBD pair1 bit7 only: tie` | `0x1b` | PASS | PASS (no keyboard) |
| `KBD Pr from its own pair` | 3 | PASS | FAIL — measured `None` (no `keyboard` row) |
| `KBD Pr ignores pair1 bit7` | 0 | PASS | FAIL — measured `None` |
| `89C51R: no keyboard arbitration` | `0x1b` | PASS | PASS (D13 guard, must pass) |

**The tie leg is a real discriminator, measured by ablation, not argued.** With
`uc51.cc:2223-2224` reverted to `l= sfr->get(IPL1) & nuof_it` (no `~IP_PAIR1` strip) and
the tree rebuilt, `irctest` reports **48/50** and the two failures are exactly
`KBD pair1 bit7 only: tie` → measured **`0x3b`**, i.e. the keyboard takes the vector from
timer 1 at a level nobody gave it, and `KBD Pr ignores pair1 bit7` → measured **3**
(`IPL1.7` and `IPH1.7` together read as L+H). No other leg changed, so those two legs are
the strip's exclusive detectors and they travel with the `uc51.cc` hunk. The tree was
restored (`grep -c ABLATION` = 0, the strip is back at `:2223-2224`) and re-verified at
50/50 after the experiment.

One trap this block exposed, worth carrying into `SIMULATION-PLAN.md` §5: **a setup that
overruns the vector it is testing reports the absence of a fix as the fix.** A first
draft wrote all twelve configuration registers before `SJMP $`, which is 39 bytes — past
the handler planted at `0x1b` — so `ljust()` padded nothing, both ISRs landed behind
`main`, and six legs "measured" that nothing vectored, in the same shape as the defect.
The block now writes only the registers that carry something and asserts
`len(main) <= 0x1b` before assembling.

## 9. Rulings, notes and limits

1. **`IP_PAIR1` placement — closed by Main: `regs51.h` wins**, no `#ifndef` guard, no
   duplicate. My `regs51.h` hunk is **exactly three `#define` lines** (`IP_PAIR1 0x80`,
   `bmKBD 0x01`, `bmKBDL 0x01`, `:101-103`, with cites) and **nothing SPI-relevant**: no
   `bmSPIE`/`SPIL`/`SPIDAT`/`SPSTA`, no `SPCON`, no vector or rank row. SpiBlock can add
   its bit-2 macros at the same place without conflict, and the pair-1 arm of
   `priority_of()` plus the ablation-proven legs above are already there for it to
   inherit (`IE1` bit 2, `IPL1`/`IPH1` bit 2, vector `0x004B`, rank 10).
2. **Idle/power-down wake (Main's item 1, deferred half).** Idle **is** handled and
   asserted: leg C10 shows a request that already exists when `PCON.idl` is set ends
   idle (the engine clears `IDL`, `uc51.cc:2151-2157`) and the handler then runs. What is
   **not** modelled and is deferred: a key pressed *while* the core sits in idle, and
   power-down exit entirely. Cause is the core, not this block — `cl_uc::tick()` only
   ticks hardware in `stGO` and the `stPD` branch says `//FIXME: tick outsiders eg.
   watchdog` (`uc51.cc:2045-2047`, verified by Main), so while idle or in power-down the
   comparator never samples a new level and no request can appear. Fixing it means
   ticking hardware in those states, a cross-cutting change (it would also make the
   watchdog bite in power-down) outside this assignment. Recorded, not papered over.
3. **No bit rows for `KBLS`/`KBE`/`KBF`, deliberately.** The brief said "their bit rows
   in `bit_tab51`"; they have none and must not get any — none of the three is
   bit-addressable (0x9c/0x9d/0x9e are not ≡ 0 mod 8), which is also what the comment at
   `uc51.cc:539` records. `KBLS.0` etc. as bit symbols would be wrong.
4. **`test/config` + `test/config/baseline/89C51R` regeneration.** `SIMULATION-PLAN.md`
   asked for it "in the same commit", and for an RD2-only element that is wrong: adding
   an element only changes `info hardware` for `89C51RD2`, there is no `89C51RD2`
   baseline (14 device rows, verified by Main), and regenerating the `89C51R` baseline
   would bake in a transcript change that `t/ident89c51r.py` (36/36) exists to forbid.
   `runtests` was left at the 11/20 upstream baseline. Accepted; plan text being rewritten
   by Main.
5. **`doc4235-spec.md` cites "pp.59-61" for section 15**; table 15-3 (`KBLS`) is on
   printed p.62 (`p061.txt`), so 59-62. Main fixed it at `:177`.
6. `hw reset()` runs before `cl_51core::clear_sfr()`, so `reset()` takes `keys` from the
   documented reset constant (`0000 0000b`) and never reads the cell back
   (`rd2_kbd.cc:195-208`); the `write()` hook makes the later `clear_sfr()` write of
   `KBF`=0 land consistently. That comment is the non-obvious part of the reset path and
   Main is carrying it into `SIMULATION-PLAN.md`.
