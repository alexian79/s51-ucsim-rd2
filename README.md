# s51 — AT89C51RD2 simulator base

Base for 8051 decompilation work. Built on **ucsim 0.8.15** (D. Drotos, GPL-2), taken from
`CrispStrobe/ucsim-stc`, selected because it already ships a dedicated **AT89C51RD2 device
model** (`s51.src/uc89c51r.cc`, class `cl_uc89c51r`).

This repo is the **simulator side**: a device-model and peripheral build-out on ucsim, a
verification bed of 25 counted suites plus `runtests` (reported, never counted), and the analysis
notes that record what the datasheet actually says. The
AT89C51RD2 Ghidra language is the sibling repo `ghidra-at89c51rd2`; the gated patch to Ghidra's
shared 8051 include is `ghidra-8051-dual-dptr`. GPL-2.0-or-later (`LICENSE`); the upstream commit
this was built on, and the licensing of every piece, are in `NOTICE`.

    git clone https://github.com/CrispStrobe/ucsim-stc vendor/ucsim-stc
    git -C vendor/ucsim-stc checkout 441710c     # the base commit this work sits on
    git -C vendor/ucsim-stc apply ../patches/rd2-vs-ucsim-stc-441710c.patch
    # the ../ is needed: `git -C` resolves the patch path against the target directory
    ./build.sh                                   # configure + make (logs: /tmp/s51-*.log)
    ./s51 firmware.hex                           # interactive; == ucsim_51 -t 89C51R
    cd t && ./floor.sh                           # 25 suites against that binary, ~2 min

The 161 MB GPL base tree is not redistributed here. `patches/rd2-vs-ucsim-stc-441710c.patch` is
verified against a fresh clone of that commit: applied there, `diff -rq` against the tree every
suite in `t/` was run against reports **no content differences** (`NOTICE` has the recipe).
`S51_ROOT` overrides where `build.sh`, `s51` and `t/floor.sh` look for the tree. Device name is
`89C51R` (`-H` lists all; `mkhex.py` builds Intel-HEX images in `t/`).

## What "RD2" actually is

The handoff assumption was wrong and is worth recording: the AT89C51RD2 is **not** a 4 KB /
128 B stock 8051. Per `doc4235.pdf` it is an 80C52-class core with:

| Item | RD2 | Stock 8051 assumption |
|---|---|---|
| Flash | 64 KB | 4 KB |
| Internal RAM | 256 B | 128 B |
| Timers | 3 | 2 |
| Interrupts | 9 sources, 4 priority levels | 5 sources, 2 levels |
| On-chip XRAM | 1792 B, software-selectable | none |
| DPTR | dual, AUXR1.0 select | single |
| Clock | X2 mode: 6 clocks/machine cycle | 12 |
| Extras | PCA, watchdog, SPI, EUART, keyboard, prescaler | — |

"RD2"/"ED2" are product variants (ED2 adds 2 KB EEPROM), not an IP errata revision.

## Modelled vs missing

Verified against `conf` / `info mem` for `-t 89C51R`:

### Dual DPTR — supported, verified

Banked at the address-decoder level by `sfr[0xa2] mask=0x1` (AUXR1.DPS). Verified: `MOV DPTR,#`
writes the selected pointer, the active pointer is marked in `info registers`, and `INC AUXR1`
toggles DPS — which works because bit 2 is stuck at 0, exactly as doc4235 §8 describes. Final
state of `t/dptr2.hex`: `DPTR0=0x5678`, `*DPTR1=0xaaaa`.

The other ways to reach AUXR1 all behave as the bit-2-stuck-at-0 rule predicts: `DEC`, `ORL`,
`ANL`, `XRL` and `DJNZ` each toggle, set or clear DPS exactly as their arithmetic on bit 0
dictates (`t/rmwtest.py`, 10/10 against both cores of this project), and `t/dpstest.py` pins the
`MOV AUXR1,#n` immediate form. Writes whose *value* is
only known at run time — `MOV AUXR1,A` and friends — are correctly reported as unresolvable;
`gh/Bank51.java` asserts the bank there once an actual run has established it (`t/banktest.py`).

### Extended IRAM — both meanings

**Upper 128 bytes (0x80–0xFF, indirect-only)** — supported. Verified: `MOV A,0A0h` returns the SFR
P2 (`0xff`) while `MOV A,@R0` with `R0=0A0h` returns the separate upper-RAM cell (`0xde`) — `t/up128.hex`. Same
address, physically distinct, routed by addressing mode.

**1792-byte expanded XRAM** — *was* only half there; fixed locally, see "Local changes". With
`AUXR.EXTRAM` cleared, MOVX below the XRS-selected size now hits a distinct 1792-byte on-chip
array and above it the off-chip array. All 10 size boundaries verified (see Verification).

### Boot ROM and the Flash API — `AUXR1.ENBOOT`, `USER_CALL`

`ENBOOT` (0xA2.5) maps the 2048-byte boot ROM over `F800–FFFF` (table 8-1, printed p.21) and is
what makes `USER_CALL` at `FFF0` answer at all: "Before executing the bootloader commands the
ENBOOT in AUXR1 register must be set", and the jump "must be done by LCALL instruction to be able
to comeback in the application" (§24.10, printed p.112). A call that arrives with `ENBOOT` clear is
refused -- named, counted by `info hardware boot`, and then the flash array's own byte at `FFF0`
executes -- so software that forgot the bit cannot pass an API test. The interception is on the
instruction boundary (a `PC == FFF0h` test in `exec_inst`), never on a read hook, because a flash
array that erased blocks in answer to a `dump code` would not be a memory. Write timing
(`tWRITE`) is deliberately 0 cycles, which is a recorded decision, not an oversight
(`t/bootrom-report.md`). The AT89C51RB2/RC2 has no boot ROM: it registers no `boot` element and
keeps the flat 64 K mapping it always had, which `t/bootromtest.py` asserts from both sides.

### Still not modelled

Confirmed by grep, and re-checked after every phase. Addresses below come from doc4235 Tables
3-2…3-11, **not** from grep hits:

- the flash work that needs a chip erase or a fuse: the BSB/SBV/SSB **security levels** and their
  monotonic clearing, the fuse bytes `0Ah`, `HSB 0Bh`, boot ID/version `0Eh`/`0Fh`, the §24.9 serial
  bootloader, parallel programming mode, the `BLJB` fuse, and a host-side backing file that would
  let the flash array survive the simulator *process* (the bytes do survive `reset` inside one run,
  which `t/bootromtest.py` asserts). Scope statement: `t/bootrom-report.md`.
- Deliberately **not** modelled, with the reason in `SIMULATION-PLAN.md` Phase 6 items 19-20: ONCE
  (§20 — there is no register; it is entered by holding ALE low through a reset release), reduced-EMI
  (`AUXR.AO` — stops ALE being an *output*, nothing inside the core changes), `AUXR.M0` (the `MOVX`
  pulse width — bus signalling, not instruction timing) and `AUXR.DPU` (port pull-ups, which this
  model has no concept of).
- a *host-side* backing file for the AT89C51ED2 EEPROM array, and the ISP-side "Program EEPROM data"
  command of table 24-9 (printed p.110): the array itself is modelled (`rd2_eeprom.cc`, see **Local
  changes** (6)) and its bytes survive `reset` inside a run, but ucsim has no VCC-removal event to
  hang load/flush on, and the silicon has no in-application erase trigger to model either.

Off this list since the audit: `CKCON0` (see **X2 mode** below), `PCON.POF` (reset `00X1 0000b` =
Table 21-1 p.87: POF is *set* out of reset and cleared by software; the power-fail comparator itself
stays unmodelled by decision), the internal baud-rate
generator (`BRL` 0x9A / `BDRCON` 0x9B — `t/euarttest.py`, 34 checks reproducing the datasheet's own
table vectors to the clock), the enhanced UART's receive side (`SM2` multiprocessor address filter,
the `uart0_rx9` 9th-bit/stop-bit input framing, and the `SMOD0`→`SCON.7` framing-error mux —
`t/uartmp.py`, 19 checks), the **keyboard interface** (see **Keyboard interface** below), and the
**SPI** together with its `CKCON1.SPIX2` scaling (see **SPI** below): `CKCON1` (0xAF) used to be
named-and-reset with no reader anywhere, and its one defined bit now scales the SPI byte clock —
like every `*X2` bit it is validated only while `CKCON0.X2` is set, so `X2` alone halves the byte
time in oscillator clocks and `X2`+`SPIX2` restores the standard rate (Table 7-2, printed p.20).

### Keyboard interface — §15, `rd2_kbd.cc`

Eight level-sensitive inputs on P1.0-P1.7 (`t/kbdtest.py`, 30 checks; `t/kbd-report.md` is the
build report). `KBLS` picks which level counts as a key, the flag `KBF.x` follows that level
**whether or not `KBE.x` is set**, and the interrupt request is `KBF AND KBE AND KBD(IEN1.0) AND EA`
at vector `0x003B`. `KBF` is read-only and clears entirely on read, which is how the datasheet's own
contradiction between "automatically cleared by reading" and "must be cleared by software" resolves
(`SIMULATION-PLAN.md` §3.2). Detection is a **level**, so holding a key re-asserts the request after
the flag is read (`D2` vs `D1`); because `P1` resets to `0xff`, writing a high-detect `KBLS` bit is
itself a key out of reset. The comparator is sampled once per machine cycle, not from a port-change
event, because it must also answer on a `KBLS` write and on leaving reset.

The second interrupt pair (`IEN1`/`IPL1`/`IPH1` 0xB1-0xB3) is now **live**: the keyboard is the first
source to take its level from `IPL1`/`IPH1`, four levels really are arbitrated
(`SIMULATION-PLAN.md` D13), and `priority_of()` strips the pair-selector flag out of the
register mask — without that, `MOV IPL1,#0x80` hands the source a level, since bit 7 of those
registers is Reserved but reads back what was written. `t/irctest.py` (50 checks) proves the strip
by ablation: removing those two lines fails exactly two legs and nothing else.

The full implemented / partial / named-but-inert / absent matrix, the defects found while building
it, and the plan to close the gaps are in **`SIMULATION-PLAN.md`**.

### SPI — §16, `rd2_spi.cc`

Master and slave on four shared Port 1 pins — `SS` P1.1, `MISO` P1.5, `SCK` P1.6, `MOSI` P1.7
(Table 4-1, printed pp.11-12) — clocking a byte in `16·BD` oscillator clocks, `BD` from the
`SPR2`/`SPR1`/`SPR0` ladder `/2 … /128` (that ladder is printed on p.70; the row block spills off
p.69). `SS` is the same pin `TEXEN` watches for a timer-2 reload, so every SPI pin write is a
read-modify-write of the `P1` latch, and `t/spitest.py` runs a leg with `TEXEN` live to prove the
two features do not corrupt each other (56 checks; `t/spi-report.md` is the build report).

All four `CPOL`/`CPHA` modes come out of one rule: a byte is 16 half-periods and the sampling edge
is chosen by `CPHA` alone — `CPOL` never appears in it — so `SPIF` lands on the eighth sample,
transition 15 with `CPHA=0` and 16 with `CPHA=1`, and the master parks `SCK` back on `CPOL` every
byte. The two requests (`SPIF` = transmitter, `MODF` = receiver/error) are `cl_it_src`s on
`IEN1.ESPI` at vector `0x004B`, taking their level from `IPL1`/`IPH1` bit 2 through the pair-1 arm
the keyboard opened. `SPSTA` writes are dropped, because all four flags are set by hardware and
every documented clearing mechanism is a *sequence* on other registers; `MODF` is a **level**,
re-asserted while `SS` stays low — the only reading that gives footnote 1 on printed p.64 meaning —
so software has to free the pin or set `SSDIS`, and with `SSDIS` set no mode fault arises at all.
`info hardware spi` prints `spi[15] …`; its counters are keyed `bytes=`, `wcol_events=`,
`modf_events=`, `sserr_events=` precisely so they cannot be confused with the flag keys above them.

Not modelled, with the reason in `rd2_spiccl.h`: the maximum slave `SCK` frequency (the datasheet
answers that twice, irreconcilably, and the clock is an asynchronous pin sampled once per machine
cycle anyway), electrical Hi-Z on `MISO`, and a transmit buffer, which the part does not have.

### Clock tree — the `CKRL` prescaler and `CKCON0`

doc4235 §6 puts an 8-bit reloadable prescaler **ahead of** the CPU/peripheral split: §6.2.1 (printed
p.16) writes its result as the single equality `FCLK_CPU = FCLK_PERIPH`, and Fig. 6-1 (p.16) and
Fig. 7-1 (p.18) both draw it upstream of the `/2`-and-`X2`-mux stage. So there is one prescaled net:
the machine cycle and the peripheral clock slow down together and their ratio — which is what every
peripheral here is written against — never moves. The law comes from the two endpoints:
**`M = 1` when `CKRL = FFh`** (the bypass, which Table 6-1 p.15 resets it to, so the part comes up at
`FOSC/2` Standard / `FOSC` X2 and `255 − CKRL = 0` is never a divide by zero) **else
`2·(255 − CKRL)`**, which reproduces `FOSC/1020` and `FOSC/510` at `CKRL = 00` in the two clock
states. `cl_uc89c51r::ckrl_divider()` (`uc89c51r.cc`) applies `M` **once**, to the value of a whole
machine cycle in oscillator clocks, inside `clock_per_cycle()` — so no consumer ever divides by `M`
itself, no helper returns a fraction, and nothing drifts. Two consequences, both measured:

- Everything that counts machine cycles is left alone and divides for free: timers 0/1/2, the `T2OE`
  clock-out, the PCA timebase, the internal BRG, the SPI bit clock, every instruction's cycle count,
  and the watchdog — §19 states its periods in machine cycles, and `CKRL` is named nowhere in
  doc4235 outside printed pp. 4, 8, 15, 16 and 18.
- The only consumers that counted raw oscillator clocks were the two fixed UART rates, so
  `serial.cc` now accumulates them in `FCLK_PERIPH` periods on the RD2 EUART. At the reset value the
  accumulator halves while each divisor halves with it, so every `acc >= div` comparison still fires
  on the same machine cycle and `t/ident89c51r.py` stays byte-identical.

`t/ckrltest.py` (35 checks) measures the law by differencing two runs whose images are byte-identical
except a NOP tail, so launch cost, setup cost and the cycle in which the `CKRL` write hook runs all
cancel: `M = 2 / 14 / 510` clock costs, X2 composition (12 and 3060 clocks per cycle), no drift over
8192 machine cycles at `M = 14`, then per-consumer clock costs for T0 (`X2|T0X2` halves while `CKRL`
multiplies), PCA `CPS=01`, a 32-machine-cycle `T2OE` period, BRG overflow pairs and the mode-2 bit.
21 of the 35 fail against the pre-prescaler binary (measured against the control build at
`/tmp/s51-ref-b`).

On top of that net, doc4235 §7 gives two independent rules, and the second one is easy to read
backwards:

- `CKCON0.X2` switches the **CPU** from 12 to 6 clock periods per machine cycle. The number of
  machine cycles an instruction retires in does not change, so `X2` alone does not make the CPU
  execute more instructions per second in a cycle-count trace — it makes each cycle worth half as
  many clocks. `clock_per_cycle()` (6 with `X2`, else the inherited 12, then `× M` from `CKRL`) is
  the hook.
- Each `TnX2` bit switches **that peripheral** between the standard 12 clock periods per
  peripheral cycle and the fast 6, and is *inert unless `X2` is set*. All six bits share one
  polarity, `WDX2` included.

The consequence is that measured in machine cycles a peripheral can only ever run *slower* than
one tick per cycle, never faster: with `X2` on and `TnX2` clear the peripheral and the CPU both
advance at 6 clocks, so the timer still ticks once per cycle; with `TnX2` set the peripheral holds
at 12 clocks and ticks once every two cycles. `peripheral_cycles(cycles, x2_bit)` returns that
count and carries the half-cycle so a slowed peripheral keeps phase instead of dropping edges.
Applied to timers 0/1/2, the PCA's machine-cycle clock source, and the watchdog.

`SIX2` is read, and it is not what its name suggests: p.19 titles it "Enhanced UART Clock
(**Mode 0 and 2**)", the UART member of the `TnX2` family, acting on the two fixed-rate modes only
and only while `X2` is set. `cl_serial::fixed_divby()` applies it to those two divisors alone, so a
BRG-driven or timer-driven rate never sees it — `t/euarttest.py` measures a BRG-driven bit period
identical with `SIX2` both ways (147.00 vs 147.00). Because those accumulators now count
`FCLK_PERIPH` periods, cleared `SIX2` — the UART following the CPU — is already the number
`serial_bit_cnt()` hands down, and **set** `SIX2` is the bit that holds the UART at standard
peripheral speed while the CPU runs twice as fast, so it *doubles* the divisor: mode 0 goes from
`FOSC/6` to `FOSC/12` in X2. The datasheet rows are unchanged — mode 0 costs one machine cycle and
mode 2 costs `FXTAL/64` with `SMOD=0` / `FXTAL/32` with `SMOD=1` (Table 14-4, printed p.53) — but
they are entered halved (mode 0 12→6, mode 2 64/32→32/16) because `FCLK_PERIPH` is `FOSC/2` in
Standard mode and `FOSC` in X2. Mode 2 was 2× too fast in this tree until 2026-11-13
(`SIMULATION-PLAN.md` D11); `t/euarttest.py` pins it at 70.91 and 38.18 measured clocks/bit against
windows derived from the table number plus the poll loop's own instruction cost, and `t/x2test.py`
(15 checks) pins the rest, each rate measured by differencing two runs so instruction overhead
cancels. Timer-driven baud in modes 1 and 3 is untouched by `SIX2` by definition.

### Peripherals — timers and UART

Verified by `t/hwtest.py` (8/8):

| behaviour | expected | measured |
|---|---|---|
| timer 0 mode 1 overflow sets TF0 | 65536 MC | 65545 MC |
| timer 0 interrupt vectors to `0x000B` | ISR entered | yes |
| timer 1 mode 2 auto-reload rate | 12 clks/count | 12 |
| timer 2 mode 0 auto-reload rate | 12 clks/count | 12 |
| UART mode 1, T1 baud, TH1=`0FDh` | 96 MC/bit = 9600 | 96 |
| UART mode 1, `PCON.SMOD=1` | 48 MC/bit = 19200 | 48 |
| UART mode 1, T2 baud, RCAP2=`0FFFDh` | 96 MC/bit = 9600 | 96 (was 8) |
| transmitted byte reaches the serial output | byte | yes |

Capturing TX needs **`-S out=<file>`** — `-I out=` is the memory interface, not the UART:
`printf 'file "f.hex"\ngo\nexit\n' | s51 -S out=/tmp/tx.bin`. Serial **receive** works from batch
mode too: with `-S in=<file>` a byte reaches SBUF and RI during `go`, because `run_go()` polls the
UART terminal console every period steps (`t/uartmp.py` pins it, leg `batch_rx_byte_per_frame`,
19/19). What batch RX cannot express is per-frame 9th/stop-bit control — the SM2 and FE rules of
doc4235 §14.1/14.2 are defined entirely in terms of those two bits — which is what the RD2-only
`uart0_rx9` input exists to supply.
Timer 2 correctly does **not** set TF2 while RCLK/TCLK are active, per the 8052 rule.

The RD2's own clock-source selection is honoured only partially: ucsim treats T2CON.RCLK/TCLK as
the T1-vs-T2 choice, which matches the datasheet table, but `BDRCON.TBCK/RBCK` and the internal
BRG (`BRL` 0x9A) are absent, so `TBCK=1` configurations would silently fall back to a timer.

## Local changes to the vendored tree

**(1) Expanded-RAM window.** The upstream 89C51R model only banks the low **256** bytes on
`AUXR.EXTRAM` — that is the Intel 51R rule, not the RD2's. The RD2 window size comes from
`AUXR.XRS2:XRS0` (doc4235 Table 9-1: 256/512/768/1024/1792, **reset 768** for T89C51RD2
compatibility), so MOVX to 0x0100–0x06FF wrongly shared a cell with the off-chip bus.

**(2) Timer 2 baud rate, 12× too fast.** `do_t2_baud()` scaled its incoming cycles by 6 while
`do_t2_reload()` did not, so the same counter ran at two different rates depending on mode; and
`cl_serial::serial_bit_cnt()` forced `_divby=16` for T2 baud where the Intel/Atmel formula needs
32. Measured 8 MC/bit instead of 96. Both are now gated on `clock_per_cycle() == 1`, which keeps
the 1T STC models byte-identical (the fork's own `rung_stc15_uart_baud.sh` still reaches
`0x000041`) and gives every 12-clock part the correct rate.

**(3) Timer 2 ignored `cycles`, so `T2X2` could not be honoured.**
`do_t2_reload()` and `do_t2_capture()` ignored their `cycles` argument and advanced the counter a
single step per tick, while `do_t2_baud()`, `do_t2_down()` and `do_t2_clock_out()` all loop over it.
This was *not* a rate bug in standard mode: `cl_uc::tick_hw()` (`core/sim.src/uc.cc:2989`) walks the
instruction's machine cycles and calls `hw->tick(1)` once per cycle, so one step per tick already
meant one count per machine cycle on a 12-clock part, whatever the instruction mix. It mattered as
soon as `CKCON0.T2X2` had to be honoured, because `peripheral_cycles()` returns 0 on the half-cycles
a 12-clock peripheral must skip, and a function that never reads its argument advances anyway. Both
now loop over `cycles` like their siblings, which is what lets `T2X2` halve timer 2 (`t/x2test.py`).

**(4) CKCON0 / CKCON1, X2 mode and the `CKRL` prescaler.** See **Clock tree** above:
`clock_per_cycle()` returns 6 when `CKCON0.X2` is set and then multiplies by `ckrl_divider()`, and
the new `peripheral_cycles()` virtual applies each `TnX2` bit.

**(5) Boot ROM, `ENBOOT` and the on-chip Flash API.** New `rd2_boot.cc` / `rd2_bootcl.h`
(`RD2_BOOT_ID 16`) register a `cl_rd2_boot` element on the RD2 only. It decodes `AUXR1.ENBOOT`
(bit 5 — the same register as `DPS` bit 0, so the two must never be conflated), installs the
2048-byte ROM over `F800–FFFF`, dispatches the table 24-10 command set behind `USER_CALL`, and
counts refused calls for `info hardware boot`. The `PC == FFF0h` test itself is in the new
`cl_uc89c51rd2::exec_inst()` — no peripheral can see an instruction boundary — and the API's
return is issued as `instruction_22(0x22)`, which is what makes the datasheet's "must be done by
LCALL instruction to be able to comeback" work and cost what a real `RET` costs.

**(6) AT89C51ED2 data EEPROM.** New `rd2_eeprom.cc` / `rd2_eepromcl.h` (`RD2_EE_ID 17`). The
datasheet puts the array at `0000h–07FFh` *of the XRAM/ERAM space*, reached with `MOVX`, with no
address or data SFR of its own (section 23, printed p.89) — so it is modelled as a per-access
overlay on the `xram` cells (`register_hw` on 2048 of them), not as a decoder swap: `decode_xram()`'s
EXTRAM/XRS banking stays exactly as it was and is what the CPU sees whenever `EEE` is clear. `EEE`
set wins over `EXTRAM`, because the datasheet states `EEE` unconditionally and the on-chip XRAM as
the conditional one. `EEBUSY` is forced on the write hook, since bit 0 is "Can not be set or cleared
by software"; `tWRITE` is 17 ms Max (table 25-2, printed p.115) converted through
`clocks_of_time()`, i.e. **time**, not machine cycles, so neither `X2` nor `CKRL` shortens it; the
byte commits only when the cycle completes and the array is "not available for reading or writing"
meanwhile, so a read answers `0xff` and a write is refused, both counted. The array survives
`reset()` (the only power-cycle-like event ucsim has) and deliberately has no backing file: ucsim has
no VCC-removal event to hang load/flush on. Registered behind `rd2_is_ed2()` in
`cl_uc89c51rd2::mk_hw_elements()`, so the plain RD2 keeps `EECON` named-and-inert as D14 pins.
`t/eepromtest.py`, 29 legs — 30 when `S51_REF` names a control binary.

**(7) Every port edge counted twice by the counters.** `cl_port::write()` and `cl_port::set_pin()`
refreshed the read-only `port_value` cfg mirror with `cfg->write()`, and `cl_hw::init()`
(`core/sim.src/hw.cc:104`) registers every cfg cell back onto the owning element — so that mirror
write re-entered the port's own hook and ran all the way to `inform_partners(EV_PORT_CHANGED)`. One
latch write emitted **two** events, and every counter subscribed to `EV_PORT_CHANGED` — timer 0,
timer 1, timer 2 in counter and capture mode, and the PCA external clock input — advanced twice per
edge. The fix is three characters per site: update the mirror with `cfg->set()`, which stores the
same value without notifying, exactly as `cl_port::init()` already did at `port.cc:105`. A
timer-side dedupe was rejected: the two events carry identical payloads, so a subscriber cannot tell
a doubled edge from two real ones. Pre-existing and device-independent — all three sites are
byte-identical in upstream `0.9.12-1-g19d932c3` (`port.cc` is unchanged there since `59a14ff9`,
so the check holds at both) — and `t/t2cnttest.py`
(30 legs, 9/30 before the fix) is what pins it.

**(8) The `break` command silently threw away part of what you typed.** `cl_brk_cmd` matches a
chain of grammar branches and then reads *fixed* parameter slots, so a branch could match a command
legitimately and discard part of it in silence. `break 0x3a if 0` matched `CELL STRING STRING` — a
bare number is a cell, and a numeric argument carries no string value — so the condition became the
empty string and the command installed an **unconditional** breakpoint that stopped at everything.
`break 0x3a iff ACC==0x5a` matched that same branch and the keyword test (`if (s == "if")`) fell
through, again unconditionally; `break 0x3a hit 2` was discarded the same way and kept `Hit 1`. A
debugger that stops where you did not ask is worse than one that refuses, so a condition argument
that is not a string is now refused with an explanation, an unknown keyword after the address is
refused, and `hit` is honoured on both the fetch and the event forms. *Refused* means nothing is
created. Note what is **not** broken here: a bracket expression inside a *quoted* condition
evaluates correctly (`if "iram[0x40]==0x5a"` fires, `…==0x5b` does not); only the unquoted form
fails, because the console splits on spaces before the parser ever sees it. `t/bpcmdtest.py`
(25 legs) pins the refusals and their messages, conditions at both polarities, `hit`, and the `var`
bindings; 6 of its legs fail on the pre-fix binary by design.

**(9) A fetch breakpoint could not be reached by single-stepping.** `cl_uc::do_brk()` tested fetch
breakpoints only under `sim->steps_done > 0`, and `steps_done` resets to 0 at every stop, so the
first instruction fetched after **every** stop was exempt — not just the one case that genuinely
needs the exemption, a CPU still sitting on the breakpoint that stopped it. Measured: 12 consecutive
`step` commands across an armed address gave zero `(104)` stops while PC walked past it, and after
`pc 0x37` + `step` landed the CPU on the armed address, a following `run` sailed straight past the
breakpoint sitting there. `step 30` did fire it, because those fetches happen mid-flight. The rule
is now address-specific: `cl_sim::stop()` records which breakpoint stopped the CPU
(`brk_stop_addr`/`brk_stop_valid`) and `do_brk()` exempts exactly one fetch of exactly that address.
Event breakpoints never shared this gate — they are checked in the memory-cell write path — which is
why their `(112)` reports appeared under bare `step` all along. `t/wptrtest.py` WP-K13/K14/K15 pin
both rules and classify which one the binary under test implements.

**(10) Two usage lines that lied.** `var`'s usage read `var name [memory addr [bit]]`, where
`memory` is a placeholder and not a keyword: typed literally it is a `Parser error`, and it always
has been — on this fork and on upstream `19d932c3` alike, so no cell was ever silently bound to the
wrong thing. What the line actually did was hide the real forms, and it now reads
`var name [mem addr [bit_nr [bit_nr_high]]] | var name mem[addr]`, with both advertised forms pinned
by `t/bpcmdtest.py`. And `-d <portnum>` — the GDB
remote server port — is back in `--help`: `app.cc` had its option text and the `DOPT` usage string
commented out while `main()` still parsed it and assigned `rgdb_port`, so a working debugger feature
was invisible.

**(11) A name from the assembler meant nothing to the simulator.** A loaded program gave you
addresses only — `break 0x3a`, `if "iram[0x40]==0x5a"` — even though the variable machinery already
had everything needed for better: `cl_var_list` indexes bindings by name *and* by address, every
binding records who created it, and the command lexer resolves any identifier to a `(memory,
address)` cell. Nothing could fill it from a build. `sym "prog.sym"` now reads the `asxx`/`sdas`
symbol list, and `file "prog"` picks up `prog.sym` beside the image the way `.cdb` and `.map`
already were. Bindings are stamped `VBY_DEBUG`, so a device, a user `var` or an analyzer binding
always wins — a `.sym` row states only what the assembler called something, and `info variables`
and `expression` print those names, which `t/ident89c51r.py` pins byte-for-byte. A symbol with no
byte cell (a bit symbol) is refused rather than parked somewhere plausible, and a malformed line
names the file and line and loads nothing after it. Area-to-memory mapping is
`cl_51core::sym_memory`, a virtual, so another family can answer differently. After it, `break
_load_acc` stops at the same instruction the listing calls `_load_acc`.

**One upstream defect this fork does not carry, and how to un-break 0.9.x.** Upstream HEAD as of
`19d932c3` SIGSEGVs during startup on every device that builds a PCA — `-t 89C51R` and `-t 251`
exit 139 before the first prompt — while `-t 51` and `-t 51R` start normally, so the natural
conclusion that the device names are wrong is false: both are registered and listed by `-H`.
`cl_hws::add()` (`core/sim.src/hw.cc:610`) calls `item->added_to_uc()` at add time, i.e. before
the owner's `init()` has run, and `cl_pca::added_to_uc()` (`s51.src/pca.cc:88`) reads the member
`sfr`, which only `cl_pca::init()` assigns. The four sibling hooks take a local `sfr`; `pca.cc` is
the only one that does not, because `6e4d431d` ("remove old consts") deleted its local line and let
the name resolve to the member. This fork is immune only because its 0.8.15 base still has that
line. Restoring it — one line, `s51.src/pca.cc` — makes all four devices boot and each fire exactly
one `(104)` at a `break 0x3a`. Bisect: `6e4d431d^` exits 0, `6e4d431d` exits 139.

| file | change |
|---|---|
| `s51.src/regs51.h` | `bmXRS` (0x1c) next to `bmEXTRAM`; `CKCON0`/`CKCON1` addresses; `bmX2`…`bmWDX2`; `bmEEE` (0x02) and `bmEEBUSY` (0x01) on `EECON` |
| `s51.src/uc51cl.h` | `peripheral_cycles()` virtual, defaulting to a pass-through |
| `s51.src/uc89c51rcl.h` | declare `make_chips`, `decode_xram`, `clear_sfr`, `rd2_mem`, `eram1792_chip`, `clock_per_cycle`, `peripheral_cycles`, `fclk_periph_per_cycle`, `ckrl_divider`, `x2_carry` |
| `s51.src/uc89c51r.cc` | 1792-byte `eram1792_chip`; seven per-page `cl_banker`s selected by EXTRAM (index bit 0) and XRS (bits 1..3); `AUXR` reset to `0x08`; `CKCON0` reset to 0 and the X2 clock model; `ckrl_divider()` multiplying `clock_per_cycle()`; `rd2_is_ed2()` gating only the EEPROM element, since the device table gives `89C51RD2` and `89C51ED2` the same CPU flag |
| `s51.src/timer0.cc` | timer 0/1/2 tick scales by `TnX2` |
| `s51.src/timer2.cc` | `do_t2_baud()` applies its `*= 6` only when `clock_per_cycle() == 1`; `do_t2_reload()`/`do_t2_capture()` consume `cycles`; tick scales by `T2X2` |
| `s51.src/pca.cc` | machine-cycle clock source scales by `PCAX2` |
| `s51.src/wdt.cc` | watchdog counter scales by `WDX2` |
| `s51.src/serial.cc` | T2-baud `_divby` = 32 on 12-clock parts, 16 on 1T parts; on the RD2 EUART the mode-0/2 accumulators count `FCLK_PERIPH` periods and `fixed_divby()` doubles for `X2|SIX2` |
| `s51.src/uc51.cc` | `CKCON0`/`CKCON1` added to the SFR name table under `CPU_89C51R` |
| `s51.src/objs.mk` | `rd2_boot.o` and `rd2_eeprom.o` beside the other `rd2_*` objects |
| `s51.src/rd2_eeprom.cc` / `rd2_eepromcl.h` | **new**: `cl_rd2_eeprom` (`RD2_EE_ID 17`), the AT89C51ED2 2048-byte data EEPROM as a per-access overlay on `xram` `0000h–07FFh` selected by `EECON.EEE`, with `EEBUSY` hardware-owned, `tWRITE` = 17 ms measured in oscillator clocks, commit on completion, and a `print_info` counter set |
| `s51.src/port.cc` | the read-only `port_value` cfg mirror is updated with `cfg->set()` instead of `cfg->write()` at the latch, bit-write and `set_pin()` sites: `cl_hw::init()` registers every cfg cell back onto the element, so the old write re-entered `cl_port::write` and emitted a **second** `EV_PORT_CHANGED` per edge |
| `core/cmd.src/cmd_bp.cc` | `cond_text()` helper; every `if (s == "if")` site refuses a non-string condition and any unknown keyword after the address; `hit` honoured on fetch and event forms; `CMDHELP` extended |
| `core/cmd.src/cmd_uc.cc` | `var` usage line spells the two real forms instead of `[memory addr]` |
| `core/utils.src/app.cc` | `DOPT` and the option text restored, so `-d portnum` appears in `--help` |
| `core/sim.src/simcl.h` | `brk_stop_addr` / `brk_stop_valid` on `cl_sim` |
| `core/sim.src/sim.cc` | `cl_sim::stop()` records the breakpoint that stopped the CPU, and clears the flag on every other stop reason |
| `core/sim.src/uc.cc` | `do_brk()` exempts exactly one fetch of exactly the address that stopped us, instead of the first fetch after any stop |

The change is gated on `type->type & CPU_89C51R` because **`cl_uc251` derives from
`cl_uc89c51r`** and must keep the old mapping — without the guard, `config 251` regressed from a
1-line to a 171-line baseline diff. With it, only `config 89C51R` changes, and its diff is purely
the new banking (126 bank lines + 11 chip/decoder lines) plus the pre-existing uninitialised-DPTR
and banner artifacts.

Items (1)-(11) are the whole delta from the vendored commit: `git -C vendor/ucsim-stc diff --stat`,
with the four configure-generated files below excluded, reports **28 tracked files, 2255 insertions,
190 deletions**; that count *excludes* the 10 untracked new files (`rd2_brg`, `rd2_kbd`, `rd2_spi`,
`rd2_boot`, `rd2_eeprom` and their headers, 2483 lines).
`patches/rd2-vs-ucsim-stc-441710c.patch` carries all **38** and is verified by applying it to a
clean clone of `441710c`: zero content differences against the tree every suite here was run
against, and a from-that-patch build passes the floor. `~/s51/rd2-fixes.patch` is the **X2-era
snapshot only** (2026-10-11, 428 lines across 10 files); it still applies with `git apply --check`
on a clean worktree of that commit, but it is not the current delta and must not be quoted as such.

Four files are deliberately **not** in the patch: `ucsim/src/common.mk`, `ucsim/src/packages.mk`,
`ucsim/src/core/utils.src/ddconfig.h` and `ucsim/docs/Makefile`. In the working tree they are
`./configure` output, and mine record *my* machine — `CURSES_LIBS` emptied, `HAVE_CURSES_H` and
`HAVE_FLEX` undefined, `prefix = /home/oleksiy/s51/install`. `build.sh` runs `configure` after
applying the patch, so a reader gets their own values; shipping mine would have switched curses off
on a machine that has it.

## Verification

**Cycle accuracy** — hand-assembled sum loop, per-instruction single-step deltas match the
MCS-51 machine-cycle table (`MOV direct,#`=24, `CLR A`=12, `MOV direct,A`=12, `MOV A,direct`=12,
`ADD A,direct`=12, `DJNZ direct,rel`=24 clocks). Prologue 72 + 10×60 = **672 clocks**, exactly
what `run` reports.

**Expanded-RAM window** (`t/xramtest.py`): all 10 boundaries of Table 9-1 exact — 0x00ff/0x0100
at XRS=000 through 0x06ff/0x0700 at XRS=100, each probed from both EXTRAM views.

**X2 mode** (`t/x2test.py`, 15/15): CKCON0 holds what was written, and every rate is measured by
differencing two reload values so the fixed instruction overhead cancels. Standard mode gives
12 clks per timer tick; `X2` with the peripheral bit clear gives 6 (same machine cycles, half the
clocks); `X2` with the bit set gives 12 again because the half-rate peripheral exactly cancels the
faster CPU; and the bit alone, without `X2`, changes nothing.

**Dual-DPTR bank resolution** (`t/rmwtest.py` 10/10, `t/dpstest.py` 2/2, `t/banktest.py` 3/3):
the same bytes run through the executing simulator and through Ghidra's static resolution, and the
two must agree. `banktest.py` deliberately records the case where Ghidra is *wrong* on its own —
a bank selected through `MOV AUXR1,A` — and only then checks that the `Bank51` override brings it
into line, so a future language that resolves the case by itself fails the middle leg instead of
silently passing.

**SPI bit clock** (`t/spitest.py`, 56/56): the byte time is asserted without any fitted constant.
The overhead-free quantity is the **cross-divider difference**, `per_byte(BD=128) − per_byte(BD=2)
= 16·(128−2) = 2016` oscillator clocks — measured 2016.0 — because one run's byte time also
carries the poll loop's fixed cost. Probe order is part of the method: `info hardware spi` reads the
SFRs through `get()` and disturbs nothing, whereas `dump sfr 0xc3 0xc5` *arms and then performs* the
SPIF/WCOL/MODF clear sequence, so every leg takes the instrument reading first and the dump after,
in one session.

**ED2 2048-byte data EEPROM** (`t/eepromtest.py`, 29 legs — 30 when `S51_REF` names a control
binary): the overlay is gated on the device-row name `89C51ED2`, so the RD2 transcripts stay
byte-identical to the pre-change binary, and that identity check is the 30th leg. `EEE` wins over
`EXTRAM`, `0800h` and above stay the external bus, `EEBUSY` is forced while a write is in flight,
reads of the array while busy return `0xff`, and `tWRITE` is `clocks_of_time(0.017)` so it comes
out right at any crystal and in either X2 state. There is deliberately no backing file: nothing in
the simulator models removing VCC, so persisting the array would be theatre.

**One count per port edge** (`t/t2cnttest.py`, 30/30, and **9/30 on the binary before the fix**,
the 21 failures each naming the over-count): `cl_hw::init()` registers every cfg cell onto its own
element, so the port's read-only mirror write `cfg->write(port_value, ...)` re-entered
`cl_port::write` and fired `EV_PORT_CHANGED` a second time per latch write. Timer 0, timer 1,
timer 2 (counter *and* capture) and the PCA ECI all advanced twice per edge; the fix is
`cfg->set()` at the three mirror sites, which stores the same value without notifying. The STC12
port path is not covered by any suite here.

**Breakpoint and `var` console grammar** (`t/bpcmdtest.py`, 25 legs on `t/isa.ihx`): the forms that
used to build an unintended *unconditional* breakpoint are refused with a message and create nothing;
conditions fire at `0x3a` for `if ACC==0x5a`, `if "ACC == 0x5a"`, `if "iram[0x40]==0x5a"` and
`var z iram[0x40]` + `if z==0x5a`, and miss at the opposite constants; `break 0x3a hit 2` reads
`Hit 2` exactly like the positional form, on fetch and event rows alike. Six legs fail on a pre-fix
binary, which is the point. Every leg is bounded by a **sentinel** breakpoint at `0x44`: a session
whose tested breakpoint never fires would simulate forever, and a simulator killed at a timeout loses
its buffered output, which reads exactly like "no breakpoint was created".

**Watchpoints as assertions, and the fetch-breakpoint stepping rule** (`t/wptrtest.py`, 27 legs,
**green on both build families**): write- and read-watchpoints stop with `(112)` at the instruction
*after* the access, against negative controls on cells the image never touches — an armed watchpoint
that never stops is paired with its `info break` row plus proof the code really ran, because silence
alone proves nothing; `break <varname>` arms two event breakpoints, one read and one write. Legs
WP-K13/K14/K15 pin the fetch-breakpoint rule and classify the binary from behaviour, not version:
`exempt-first` (upstream — 15 bare `step`s across an armed address give zero `(104)`) versus
`address-specific` (this fork — one `(104)`, PC parks). Everything counts stop **codes**: `(104)`
fired, `(109)` a step merely landed there, `(112)` an event break; `Stop at <addr>` is printed in
every one of those cases.

**Names from a real build** (`t/symtest.py`, 14 legs): a `.sym` loads by command and as a sibling of
`file "prog"`; `break _load_acc` then stops exactly once with `(104)` at `0x3a`, and a **control
leg** runs the same command with no symbols loaded and requires that nothing is created — an armed
breakpoint that fires only means something if it cannot fire by accident. `expression _counter`
equals `expression iram[0x30]`; a fixture that claims `ACC` lives at `iram[0x30]` cannot move `ACC`,
which still reads `sfr[0xe0][7:0]`, while `_counter` reads the cell the file named; a malformed line
reports its own line number and binds nothing after it. The fixtures are hand-written to `asxx`
`lstsym()` output — both the `-w` one-per-line shape and the three-per-line `|` shape — because
`sdas8051` is not installed here, and `t/symtest-notes.md` says so plainly along with the gap that
remains: `read_map_file` still parses the legacy ASlink map, not modern `sdld` output.

**A scripted debugger** (`tools/s51dbg.py` and `t/s51dbgtest.py`, 68 legs, **green on both build
families**): the driver owns a `-Z` session and answers in JSON verbs, asserting on stop codes and on
`info break` re-reads rather than on what a command replied — because `delete 0x3a` prints a bare
`Error` and leaves the row (the id from `info break` is what deletes, and no reply — not even a
bare `Error` — is evidence that the row changed), a breakpoint that has never run
prints `Hit 1  Cnt 1`, and `Cond` reads `true` for a breakpoint with no condition at all. A load is
proven by ROM readback against the bytes parsed out of the `.ihx` itself, with a tamper leg that must
fail and a non-HEX input that must be refused. Where the two build families genuinely differ —
fetch-breakpoint stepping, the malformed-condition refusals, the `hit N` keyword — the suite probes
the binary once and asserts the rule-appropriate outcome, so one file is green on either. Eight legs
pin the console itself: a load is reported over a pipe as readily as over a pty and with `-c -`
dropped, so only `-q` and the `-Z` telnet console suppress it — and a byte total is never the
evidence, because both report lines echo the path that was typed.

**Upstream testsuite** (`t/runtests.py`, replicates `src/test-lib.mk` and its ELIDE rules):
11/20 baselines byte-identical. The failures are not model defects:

- `config` (1 line each): DPTR holds **uninitialised** values at reset; the baseline captured
  different garbage (`DPTR1=0xbc56` matches this build exactly).
- `bander` (bit-addressable IRAM): every value matches; only the width of the `set mem` echo and
  the ASCII column differ between ucsim versions.
- `banker`/`mdu`/`symdis DS390F`: need `sdas8051`/`sdcc` to build their `.ihx`, which is not
  installed.

**Cross-validation against an independent core** — the same linked image run on ucsim's 89C51R
and the Rust `i8051` crate (via `mcs51-rs`, driven by `xval/`), comparing
`(pc, machine_cycles, acc, psw)` before every one of 104 retired instructions:

| field | agreement |
|---|---|
| pc | **104/104** |
| machine cycles | **104/104** |
| acc | 100/104 |
| psw | 61/104 |

Control flow and timing are fully corroborated. The PSW differences are **two bugs in the Rust
core, not in ucsim**, checked against the primary source (`doc4316.pdf`, the Atmel C51 Hardware
Manual that doc4235 defers to — doc4235 itself contains no flag semantics):

- `ADD`: manual says *"OV is set if there is a carry-out of bit 6 but not out of bit 7, or a
  carry-out of bit 7 but not bit 6"*. For `0xFF + 0x02` both carries occur, so **OV=0**.
  ucsim gives `PSW=0xc1` (OV=0) ✓; i8051 gives `0xc5` (OV=1) ✗. The manual's own worked example
  (`0xC3 + 0xAA` → `0x6D`, CY=1, AC=0, OV=1) is reproduced exactly by both cores.
- `SUBB`: manual says *"AC is set if a borrow is needed for bit 3"*. For `0x00 − 0x01` there is a
  bit-3 borrow, so **AC=1**. ucsim gives `0xc0` ✓; i8051 gives `0x80` (AC=0) ✗. This stale AC then
  propagates through flag-preserving instructions, which is why it accounts for many rows, and it
  also explains the `DA A` divergence (`0x9f` vs `0x99`).
- The remaining `acc` difference is `DIV AB` with `B=0`, where the manual explicitly says the
  results are *undefined*; both cores set OV and clear CY as specified.
- `cpu8051` stops on a self-looping `SJMP` unless `-d` (disable break-on-lock) is given; its
  verbose trace has no PSW column, so read flags by executing `MOV A,PSW` and reading `acc` on
  the following line.

`cpu8051`, a third independent core, measured the same way: `ADD` → `0xc5` (OV=1, wrong) and
`SUBB` → `0xc4` (AC=1 correct, OV=1 wrong). Both non-ucsim cores therefore share a bad ADD/SUBB
overflow expression. **ucsim is the only one of the three matching the Atmel documentation on
both cases.**

**Running everything** — `t/floor.sh [binary]` runs the 25 simulation suites against one binary in
about two minutes. The binary is the **first argument**; the floor exports it as `S51_SIM`, and every suite
now reads that variable. That was not true until the SPI cutover: eleven suites hard-coded the
vendor path, so a floor "run against another build" was silently re-testing the vendor tree for
most of its rows — the tell was `ident89c51r` printing the vendor path inside its own `PASS` line.
That suite carried a worse hole than a wrong path: five of its nine images were read from `~/s51/t/`
and four from `/tmp`, it skipped any image it could not open, and it divided by the number of legs
it had actually run — so on a machine that had never seen this work it printed `PASS: 0/0`, exited
0 and left the floor green, and on the author's machine stale `/tmp` images let it report `36/36`
that a fresh clone could not reproduce. It now builds every image it needs itself, fails naming the
path and `errno` if one cannot be opened, and its denominator is fixed at images times legs.
`S51` is exported too, as the repo root, for the suites that want a directory rather than a binary.
The count is the number of suites that are **not** green, and `runtests` is **reported but never
counted**: it exits non-zero by design at its upstream `11/20` baseline, so a count of 1 has never
meant runtests -- every report that read it that way (including the ones in this repo's own phase
notes) was misreading it. Two more rules, both learned from a red suite that looked green: the
summary printed for a suite is the **last** line matching the verdict pattern, because a suite that
prints `ok` rows and then `FAIL: 48/50` used to show its first `ok`; and a single `FAIL` line makes
a suite red even when it exits 0. That is how `irctest` sat at 48/50 from the SPI cutover onward --
its two legs still expected the pre-RD2 polling order, in which the keyboard interrupt is last,
while doc4235 table 17-2 (printed p.73) puts the SPI at priority 10 and the keyboard at 8.
A suite for a block that has not been cut over yet must fail **fast**, with a named reason, never
hang. Outside the
floor are `t/langtest.py` and `t/rmwtest.py`, which start a headless Ghidra and must run **one at a
time** (fixed project/output names collide, and the symptom is a suite collapsing to a few legs), and
`t/xcompare.py`, which starts no Ghidra — it drives the Rust `i8051` core through `~/s51/xval` — so it
may run alongside either of them, but never twice at once, since both write the same `/tmp/xval.ihx`.

## Reproducing

    ./floor.sh                        # 25 suites vs one binary, ~2 min; $1 = binary to test
    ~/s51/build.sh                    # (the lines below assume the tree is at ~/s51)
    cd ~/s51/t
    ./uctrace.py tC.hex 4                # manual's worked example -> 6d 85
    ./uctrace.py tD.hex 3                # ADD overflow            -> psw c1
    ./uctrace.py tE.hex 2                # SUBB auxiliary carry    -> psw c0
    ~/s51/xval/target/release/xval tD.hex 3   # same image, i8051 core (psw c5)
    ./xcompare.py isa.asm 104            # per-instruction cross-check
    ./runtests.py                        # upstream baselines
    ./xramtest.py                        # expanded-RAM window: 10/10 boundaries
    ./hwtest.py                          # timers + UART vs datasheet: 8/8
    ./x2test.py                          # CKCON0 X2 clock modes: 15/15
    ./dpstest.py                         # dual DPTR oracle: 2/2 (A/B, bank on vs NOPed)
    ./langtest.py                        # Ghidra language: the workbench README's gate table
                                         # carries the leg count (builds the language itself)
    ./rmwtest.py                         # RMW bank forms: 10/10, ucsim vs Ghidra must agree
    ./banktest.py                        # dynamic bank + Bank51 override: 3/3

`x2test.py` and `hwtest.py` need only the simulator. The last four cover the Ghidra side.
`dpstest.py` pins what the hardware does, `langtest.py` pins that Ghidra agrees, `rmwtest.py` runs
the same bytes through both the executing simulator and Ghidra's static resolution and requires
them to agree — a one-sided test would miss a regression on either side — and `banktest.py` covers
the one case static resolution cannot reach, where the override value has to come from the
simulator. `rmwtest.py` and `banktest.py` drive `analyzeHeadless`, so each takes about half a
minute; they need `~/s51/langbuild` to have been run.

`langtest.py`, `rmwtest.py` and `banktest.py` also ship inside `ghidra-at89c51rd2`, where the
language they gate lives; `$S51_SIM` aims the simulator legs at any build. But the `langtest.py`
shipped here cannot run as-is: it executes `$S51/langbuild` (`t/langtest.py:46`), which lives in
the language repo and not in this one — point `S51` at the `ghidra-at89c51rd2` checkout, or run
the copy that ships beside `langbuild`.

`symmaptest.py` is the fourth Ghidra gate and it ships **here**, not in the language repo,
because half of it is the simulator: it gives one `.sym` to ucsim's `sym` command and the same
bytes to `gh/LoadSym51.java` inside Ghidra, then fails on any name the two tools put in
different memories. `bring-up.sh` runs it with `S51` aimed at this checkout, so the copies it
executes are the ones in this repo.

`uctrace.py` prints `T <pc> <machine_cycles> <acc> <psw>` sampled before each retired
instruction; `xval` prints the same tuples from the other core, so the two are directly diffable.

## Layout

    README.md                  this file
    SIMULATION-PLAN.md         feature status, every defect found (D1..D15), the phase plan,
                               verification rules -- the long form of everything below
    doc4235-spec.md            what the datasheet says, section + PRINTED page for each answer
    ucsim-periph-pattern.md    how to add a peripheral to this codebase, learned from the four
    t/                         floor of 25 counted suites plus runtests (reported, never
                               counted) + the Ghidra-side suites, images, mkhex.py,
                               uctrace.py, xcompare.py, runtests.py
    gh/                        GhidraScripts and the hex fixtures they read (duplicated in the
                               language repo so each repo runs alone)
    xval/                      my Rust driver for the independent i8051 core; needs
                               vendor/mcs51-rs cloned into place first
    patches/                   the whole delta as one patch against the upstream commit
    slices/                    the two upstream-sized changes, each with patch, text, evidence
    build.sh  s51              build and front end; S51_ROOT overrides the tree location
    LICENSE  NOTICE            GPL-2.0-or-later; provenance, third-party trees, what is missing

## Caveats

- ucsim's `s51` front end rejects Intel-HEX **type 05** records (`Unknown record type 5`). Harmless
  for images that start at 0x0000; strip them for others.
- `-e` commands execute **before** the positional file is loaded. Use `file "x.hex"` on the console
  or rely on `run <start> <stop>` for synchronous execution; `go` is asynchronous.
- `cpu8051` breaks on a self-looping `SJMP` unless `-d` is given, and its verbose trace has no
  PSW column — read flags by executing `MOV A,PSW` and reading `acc` on the following line.
- Ghidra 12.1 renamed several model APIs: `MemoryBlock.getStartAddress()` is `getStart()`,
  `Memory.getBlocks()` returns an array (no `MemoryBlockIterator`), `CommentType` has no `BLOCK`
  (use `PRE`), and `GhidraScript` has no `getRegisterList()` — iterate
  `getLanguage().getRegisters()`.
- `analyzeHeadless -noanalysis` leaves the listing **empty**; a postScript must disassemble itself.
  `Dptr51.java` does, and also functions the ROM start, because the 8051 analyzer never functions
  the reset vector.
- Ghidra prefixes `GhidraScript.println` output with `INFO`, so filtering log noise must not use a
  bare `grep -v '^INFO'` or script output disappears.
- The custom Ghidra language lives in `~/s51/lang` but must be **installed** into
  `$GHIDRA/Ghidra/Processors/AT89C51RD2` to be discovered; run `~/s51/langbuild` after any edit and
  after any Ghidra upgrade (`langbuild --clean` removes it). Ghidra silently ignores a module
  missing `Module.manifest`, `lib/`, or an ldefs in its current schema — the only symptom is
  `Unsupported language`, with the reason in `~/.config/ghidra/<ver>/application.log`.
- `gh51` uses `AT89C51RD2:BE:16:default`; set `S51LANG=8051:BE:16:default` for the stock behaviour.
- `DEC direct` is `0x15 dir`. `0x14` is `DEC A` and `0x18-0x1F` are `DEC Rn`; using the wrong one
  sends the simulator down a different decode path and looks like a language bug rather than a
  test bug. There is no `DEC` or `CPL` with a bit operand, and no `CPL direct` at all.
- Stepping in the simulator counts **retired instructions**, not bytes, so replacing a 3-byte
  instruction with three 1-byte NOPs changes the count. Dumping before the final store reads
  uninitialized IRAM and looks like a wrong answer instead of a short run.
- X2 mode changes **clocks per machine cycle, not machine cycles per instruction**. A trace that
  counts machine cycles looks identical with `X2` set; only elapsed time and anything derived from
  it (baud rates, timer overflow timing) move. Reading `X2` as "instructions run twice as fast"
  gives the wrong expectation for every test of it.
- All six CKCON0 peripheral bits share one polarity — cleared is the fast 6 clocks, set is the
  standard 12 — including `WDX2`, whose wording invites the opposite assumption. And all six are
  inert while `X2` is clear, so a program that sets `T2X2` alone changes nothing at all.
- Measuring a timer rate by running a loop and dividing is wrong here: several of the timer-2
  modes in this fork advanced one count per *tick* rather than per machine cycle, so the result
  depended on how many instructions the loop happened to contain. Difference two runs that vary
  only the reload, as `hwtest.py` and `x2test.py` do.
- `Bank51.java` takes its arguments from the environment, not `-scriptArgs`, because `gh51` does not
  forward them: `BANK51_ADDR=0x16 BANK51_BANK=0`. With neither set it only reports the bank each
  instruction resolved to, which is the useful mode for finding where static resolution gave up.
- The language resolves a bank write only when its **value** is a parse-time constant. `DJNZ
  AUXR1,rel` qualifies (the effect depends on nothing but the register), `MOV AUXR1,A` does not,
  and no amount of context plumbing changes that — it needs the override, with the value taken from
  an actual run.
