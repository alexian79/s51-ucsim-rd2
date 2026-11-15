# AT89C51RD2 — what is simulated, what is not, and the plan to finish it

Audit of the `89C51R` device model in the vendored ucsim 0.8.15 fork against
Atmel doc4235K (05/08). Every row cites either a code location, a datasheet
table, or a run against the built simulator. Claims that are reasoning rather
than observation are marked *[inference]*.

Status vocabulary:

| status | meaning |
|---|---|
| **MODELLED** | behaviour implemented, and this repo has a test pinning it |
| **PARTIAL** | implemented, but a documented rule is missing or wrong |
| **INERT** | the SFR is named and reset, so traces read correctly, but no behaviour follows a write |
| **ABSENT** | no define, no name, no model |

Two companion extracts feed the phases below. Per-feature behavioural detail — bit
rules, reset values, formulae, the Table 24-10 IAP command list — live beside this file in `doc4235-spec.md` (per-feature bit rules, reset values,
formulae, the Table 24-10 IAP command list, and 12 documented datasheet voids) — the
reading for Phases 2-5. The simulator-side recipe — adding a `cl_hw`, how time arrives,
raising an interrupt, in-tree test layout, plus a worked SPI walkthrough — is
`ucsim-periph-pattern.md`. Where the two disagree, this file wins: every claim below was
re-checked against the datasheet pages, and two of the spec's §3 assertions turned out
wrong (§3.1).

---

## 1. How the model is assembled

Know this before touching anything; it decides where each feature goes.

| layer | location | note |
|---|---|---|
| device name → flags | `core/utils.src/globals.cc:380` | `{"89C51R", CPU_89C51R, CPU_CMOS, "89C51R", "cmos"}` — `-t 89C51R` |
| flag enum | `core/utils.src/stypes.h:123-140` | 8051 flags occupy bits `0x0001`…`0x8000`; **`0x8000` (CPU_STC12) is the last one**, field is `enum cpu_type` → free bits start at `0x10000` |
| flags → model class | `sims/s51.src/sim51.cc:94-97` | `case CPU_89C51R: return new cl_uc89c51r(...)` |
| class chain | `cl_uc` → `cl_51` → `cl_51r` → `cl_52` → **`cl_uc89c51r`** (`uc89c51rcl.h:38`) | `cl_uc251` also derives from `cl_uc89c51r` (`uc251.cc:40`) — this is why every RD2 rule needs the `rd2_mem()` gate (`uc89c51r.cc:92-96`) |
| peripherals | `uc89c51r.cc:64-73` | `cl_uc52::mk_hw_elements()` then `cl_wdt(this, 0x3fff)` and `cl_pca(this, 0)` |
| timers | `uc51.cc:978`, `uc52.cc:64` | timer 2 arrives as `cl_timer2(this, 2, "timer2", t2_default\|t2_down)` — **no `t2_clock_out`** |
| memory | `info memory` | `rom_chip` 64K, `iram_chip` 256, `xram_chip` 64K, `sfr_chip` 128, `eram_chip` 256, `eram1792_chip` 1792, `dptr_chip` 24 |
| interrupt sources | `cl_it_src` list built in `interrupt.cc:73-115`, `pca.cc:90+`, `serial.cc`, `timer0.cc`, `timer2.cc` (17 total) | a source = (priority mask, enable cell+mask, flag cell+mask, vector, name, rank). List order is the hardware polling order — `pca.cc` deliberately uses `add_at(4, …)` so PCA sits at rank 5, matching Table 17-2 *[inference: order, not a documented API]* |
| priority | live hook `cl_51core::priority_of()` + the scan in `do_interrupt()` (`uc51.cc:2209`, `2104`); `cl_uc89c51r::it_priority()` is dead code with zero callers | RD2 gets the two-bit level and highest-wins arbitration (§3, D13); every other part keeps the one-bit first-eligible scan byte for byte |

`info hardware irq` on `-t 89C51R` prints exactly 17 sources: EX0, T0, EX1, T1,
PCA counter, PCA module 0-4, RX, TX, T2.EXF2, T2.TF2, and three STC leftovers
(`PCA_CF`, `PCA_CCF1`, `PCA_CCF0`).

### 1.1 Contracts that shape every new peripheral

| contract | where | consequence |
|---|---|---|
| `tick_hw()` walks the instruction's machine cycles calling `hw->tick(1)` once per cycle; the 51 core only forwards | `core/sim.src/uc.cc:2989`, `uc51.cc:1858` | **`tick()` always receives `cycles == 1`.** Sub-cycle rates live in your own accumulator. In-tree idioms: modulo prescaler `stc12_brt.cc:74-79`, countdown deadline `stc12_adc.cc:90-96`, consumer-owned bit-phase accumulators `serial.cc:249-292`. `clock_per_cycle()` converts cycles→oscillator clocks; it does not scale `tick()`. |
| no peripheral event queue (`event_after` absent); `hw_event` is point-to-point notification | `core/utils.src/stypes.h:433-440` | cross-peripheral work goes through `make_partner()` in `init()`, `inform_partners(EV_OVERFLOW)`, and a `happen()` that re-checks `where->id` — the baud-source pattern at `serial.cc:366-397`. |
| `init()` must chain `cl_hw::init()` before registering cells | pattern doc §1.3 | otherwise the cells are not bound and writes silently miss the model. |
| a peripheral also touches `s51.src/objs.mk` (`OBJECTS`) | `s51.src/objs.mk:5-12` | omit it and the link keeps silently succeeding without the new object. |
| `test/config/baseline/89C51R` records the hardware list | `s51.src/test/config/Makefile` | **`add_hw()` invalidates the baseline of any device that has one**, so an element registered for a baselined part needs the baseline regenerated in the same commit or `runtests` reports a false regression. There are 14 baseline rows (`251 51 517 51R 52 88X 89C51R C51 C52 C521 DS320 DS390 DS390F F380`) and **no `89C51RD2` row**, so an element gated on `CPU_89C51RD2` — the BRG, the keyboard, the SPI — must NOT trigger a regeneration: regenerating `89C51R` would bake in a transcript change that `t/ident89c51r.py` exists to forbid (`t/kbd-report.md` §8.5). Its `step 20` leg now also runs `info hw kbd` and `info hw spi`: since `info hw <category>` prints *only* the usage line for a category the part lacks, an element leaking into the shared `mk_hw_elements()` shows up as a block where `/tmp/s51-ref-b` prints the usage line — the one leak detector that exists, because there is no list-all form of the command (`t/spi-report.md` §6). |
| `reset()` must zero every remainder | `uc89c51r.cc:212-214` | a half cycle held back across a reset is void; `x2_carry` is cleared for exactly this reason. |

This is also why the timer-2 story in `README.md` item (3) is stated the way it is: with
`tick(1)` per machine cycle, "advance one step per tick" was *already* one count per cycle, so
ignoring `cycles` was invisible until `peripheral_cycles()` started returning 0 for the half-cycles
`T2X2` must skip.

---

## 2. Feature status

### 2.1 Core and memory

| feature | doc4235 | status | evidence |
|---|---|---|---|
| 80C52 core, 256 B IRAM, upper-128 indirect-only | §3, §9 | **MODELLED** | `up128.hex`; `iram_chip` 256 + separate `sfr_chip` at 0x80 |
| 64 K flash as code space | §1 | **MODELLED** as plain ROM; writable **only** through the API below | `rom_chip` 0x0000-0xffff; no console or `MOVX` path writes it (see §2.7), the IAP commands re-program it |
| 2048 B boot ROM, `AUXR1.ENBOOT`, on-chip Flash API | §24; table 8-1 p.21; table 24-10 p.112 | **MODELLED** (Phase 5 items 14-15, 2026-11-14) | `cl_rd2_boot` (`rd2_boot.cc`, `RD2_BOOT_ID 16`): `ENBOOT` (0xA2.5) decoder, ROM mapped over `F800–FFFF`, `USER_CALL` at `FFF0` dispatching READ ID (×4) / ERASE BLOCK / PROGRAM DATA BYTE / PROGRAM DATA PAGE, refused by name and counted when `ENBOOT` is clear, `RET` issued by the model so the mandatory `LCALL` returns; `t/bootromtest.py` 70/70. **NOT** modelled: security bytes `05h`/`06h`/`07h`, fuses and HSB `0Ah`/`0Bh`, boot ID/version `0Eh`/`0Fh`, §24.9 serial bootloader, `tWRITE` (0 cycles by decision), host-side backing file |
| 1792 B XRAM, `AUXR.XRS` window + `EXTRAM` | §9, AUXR `8Eh` bits 4:2 / 1 | **MODELLED** | `decode_xram()` (`uc89c51r.cc:157-195`), `xramtest.py` 10/10 |
| dual DPTR, `AUXR1.DPS` bit 0 | §8 | **MODELLED** | `cl_banker` via `cfg_set(uc51cpu_aof_mdpc, 0xA2)` + mask 1 (`uc89c51r.cc:52-58`); `dptr2`/`rmwtest` 10/10/`banktest` 3/3 |
| `AUXR1.GF3`(3), `PCON.GF0/GF1` | Table 3-2 | **INERT** (acceptable — general flags are RAM by definition) | no reader |
| `AUXR.AO`(0), `AUXR.M0`(5), `AUXR.DPU`(7) | Table 3-2 | **NOT MODELLED — decided 2026-11-13** | every effect is on a pin or on bus signalling (`M0` is the one that becomes real if a bus-level model ever lands); reasons in Phase 6 item 20 |
| `P4` `C0h`, `P5` `C7h` | Table 3-3 | **MODELLED** (Phase 1 steps 7-9) | RD2-only name rows at `0xc0`/`0xc7`/`0xe8` (`uc51.cc:220-226`) with `RD2_P4`/`RD2_P5`/`RD2_P5_BIT` in `regs51.h:104-109`, so DS390's `P5CNT` at `A2h` keeps its own name; `rd2registers` pins both latches at `0xff`, `langtest` pins that `E8`/`C7` both resolve to `P5` |

### 2.2 Clocking

| feature | doc4235 | status | evidence |
|---|---|---|---|
| X2 = 6 clocks/machine-cycle | §7, `CKCON0.0` | **MODELLED** | `clock_per_cycle()` (`uc89c51r.cc:203`), which multiplies its answer by `ckrl_divider()` |
| `T0X2`/`T1X2`/`T2X2`/`WDTX2` hold peripheral at 12 clks | `CKCON0` bits 1,2,3,6 | **MODELLED** | `peripheral_cycles()` (`uc89c51r.cc:215`) applied in `timer0.cc`, `timer2.cc`, `wdt.cc`, `pca.cc:215` |
| `PCAX2` on the PCA timebase | §7 + §13 CPS=01 | **MODELLED** | `pca.cc:215` (`CPS=00`, `1/MC`) and `pca.cc:222` (`CPS=01`, `3/MC`) both route through `peripheral_cycles(cycles, bmPCAX2)` — halve to the surviving peripheral cycles first, *then* take the 3 counts — while the `T0`-overflow and `ECI` sources are deliberately left alone because they are not machine-cycle derived. `t/x2test.py` pins all three combinations (`PCAX2` clear → same counts at half the clocks; set → 1 tick per 2 MC; inert without `X2`) and `t/ckrltest.py` the `CPS=01` clock cost |
| `SIX2` (bit 4) | §7 p.19 | **MODELLED** (2026-11-13, with item 18) | Was decoded with zero readers, and it is *not* a BRG bit: p.19 titles it "Enhanced UART Clock (**Mode 0 and 2**)", so it scales only the fixed mode-0/mode-2 rates, never the timer- or BRG-driven mode 1/3 clocks. `cl_serial::fixed_divby()` reads it, with the **opposite polarity** to its name: once the two accumulators count `FCLK_PERIPH` periods, cleared `SIX2` already leaves a bit at one machine cycle, and *set* is the bit that holds the UART at standard peripheral speed while the CPU runs at 6 clocks per cycle, so it doubles. `t/ckrltest.py` pins mode 2 at 992.73 clocks, `X2` → 496.36, `X2|SIX2` → 939.27, and `SIX2` without `X2` doing nothing |
| `CKCON1.SPIX2` (bit 0; it is the only defined bit) | Table 3-2, Table 7-2 (printed p.20) | **MODELLED** (Phase 4, 2026-11-13) | `rd2_spi.cc` scales the byte clock by it, validated only while `CKCON0.X2` is set, so `X2` alone halves the byte time and `X2`+`SPIX2` restores the standard rate; `t/spitest.py` pins all three combinations |
| `CKRL` `97h` clock prescaler | §6.1 Table 6-1 (printed p.15) + §6.2.1 (printed p.16) + §7 Fig. 7-1 (printed p.18) | **MODELLED** (Phase 6, 2026-11-13) | `ckrl_divider()` (`uc89c51r.cc:170`) multiplies `clock_per_cycle()` (`:203`) **once**, because both figures put the prescaler *ahead of* the CPU/peripheral split and §6.2.1 writes one equality `FCLK_CPU = FCLK_PERIPH`. Nothing that counts machine cycles consults it — timers, `T2OE`, PCA, BRG, SPI and the watchdog divide in time for free — and the only consumer that counted raw oscillator clocks was the pair of fixed UART rates. No clock-output pin (D7). Full derivation and the SPI/watchdog verdicts in Phase 6 item 18; `t/ckrltest.py` 35/35 |

### 2.3 UART (EUART)

| feature | doc4235 | status | evidence |
|---|---|---|---|
| modes 0-3, classic baud from T1/T2, `PCON.SMOD1` doubling | §14 | **MODELLED** | `serial.cc:167-206` `_divby`; `hwtest.py` 8/8 incl. both T2-baud and SMOD |
| internal BRG: `BRL` `9Ah`, `BDRCON` `9Bh` (`BRR` run gate, `TBCK`/`RBCK` per-direction select, `SPD` ÷6 bypass, `SRC` also feeds mode 0) | Table 3-7, §14 | **MODELLED** (Phase 2) | `rd2_brg.cc` registered from `cl_uc89c51rd2::mk_hw_elements()`, picked up by `cl_serial` through `new_hw_added()`; `t/euarttest.py` 38 checks pin the Tables 14-5/14-6 vectors, the `BRR` gate, `TBCK`/`RBCK` making `TCLK`/`RCLK` don't-care (22 references, Table 14-3 quoted at `:283`), the `SPD` bypass and the `SRC` mode-0 select. Before this, `TBCK=1`/`RBCK=1` silently fell back to a timer |
| BRG clock = `FCLK_PERIPH` = `Fosc/2` Standard, `Fosc` in X2 | §6 + Fig. 14-5 | **MODELLED** (Phase 2) | `rd2_brg.cc`; `t/euarttest.py` 38 checks reproduce Tables 14-5/14-6 to the clock, which is the only reading that does so (§5 vectors, hand-checked). Machine cycle is `Fosc/12`/`Fosc/6`, so the ratio is **one BRG count per 6 machine cycles, identical in both modes**; `SPD`=1 removes a further ÷6 |
| multiprocessor addressing: `SADDR` `A9h`, `SADEN` `B9h`, `SM2` | §14 | **MODELLED** (Phase 2, steps 12-13) | named under `CPU_51R|CPU_89C51R|CPU_251` (`uc51.cc:192,194`), reset to 0 (`uc51r.cc:109-119`), and read by the receive path in `serial.cc`; `t/uartmp.py` 19 checks run the address-match matrix (including `SADEN` don't-care bits and `SM2` gating on data vs address frames) |
| `SCON.7` is a mux: `PCON.SMOD0`=1 makes it `FE` (framing error) and hides `SM0` | §14 pp. 48, 53 | **MODELLED** (Phase 2, steps 12-13) | `t/uartmp.py` pins the whole rule: a bad stop bit sets `FE`, `FE` is sticky (a later good stop bit does *not* clear it — the suite quotes the datasheet sentence and asserts it), and with the feature on `RI` moves off the last data bit onto the stop bit; the 9th-bit/stop-bit input is fed by the `-S in=` batch channel |
| RX from batch mode | — | **BROKEN** | `serconf_check_often` registration commented out at `serial.cc:88-92` (README) |

### SPI (§16) — matrix added with the Phase 4 cutover, 2026-11-13

| feature | doc4235 | status | evidence |
|---|---|---|---|
| `SPCON` `C3`, `SPSTA` `C4`, `SPDAT` `C5` names + reset (`0x14`, 0, 0) | Tables 3-8, 3-11 | **MODELLED** | names/reset from Phase 1 (`rd2registers` 288/288 pins them), behaviour from `rd2_spi.cc` |
| byte clock: `BD` from the `SPR2`/`SPR1`/`SPR0` ladder, `/2 … /128`, byte = `16·BD` oscillator clocks | §16.2, Table 16-3 continued on printed p.70 | **MODELLED** | `t/spitest.py` asserts the overhead-free cross-divider difference `16·(128−2) = 2016` clocks, measured 2016.0 — no fitted constant anywhere |
| `SPR`=111 | Table 16-3 "Invalid" | **MODELLED as no-clock** | `divider()` returns `half=0`: no edge, no `SPIF`, byte stays loaded. Literal reading; one line changes it if real silicon is found to mean /256 |
| all four `CPOL`/`CPHA` modes | §16.2.3 p.64, figures 16-4/16-5 p.67 | **MODELLED** | the sampling edge is chosen by `CPHA` alone (`CPOL` never appears in `sample_edge()`); `SPIF` on the 8th sample = transition 15 with `CPHA=0`, 16 with `CPHA=1`; master parks `SCK` on `CPOL` |
| `SPIF`/`WCOL`/`SSERR`/`MODF` and their clearing *sequences* | §16.3.3 p.68 | **MODELLED** | `SPSTA` writes are dropped (all four bits are hardware-set, every documented clear is a sequence on other registers); `SPIF`/`WCOL` clear on `SPSTA` read then `SPDAT` read-or-write, `MODF` on `SPSTA` read then a **write** to `SPCON`, `SSERR` only on `SPEN` 1→0 |
| `MODF` as a level; `SSDIS` gate | footnote 1 p.64, Table 16-3 bit 5 (p.69) | **MODELLED** | re-evaluated every master tick, so it re-asserts while `SS` stays low; with `SSDIS`=1 `SS` is a general pin and no fault arises at all — `spitest` runs both branches and asserts `SPEN`/`MSTR` are cleared by the fault |
| `WCOL` does not disturb the running byte | §16.3.3.2 | **MODELLED** | the colliding write is discarded, the transfer continues, no interrupt |
| four signals on Port 1: `SS` P1.1, `MISO` P1.5, `SCK` P1.6, `MOSI` P1.7 | Table 4-1, printed pp.11-12 | **MODELLED** | driven by read-modify-write of the `P1` cell with `set()`, so the four foreign bits (`T2EX`, `ECI`, `CEX0..2`) survive SPI traffic; a leg runs `TEXEN` (timer-2 reload on `T2EX`, the *same pin* as `SS`) alongside, and `set()` means SPI edges cannot raise a PCA capture on `CEX3`/`CEX4` |
| two requests, transmitter and receiver/error | Fig. 16-7 p.69, Table 17-2 p.73 | **MODELLED** | two `cl_it_src`s on `IEN1.bmESPI` → `004Bh`, level from `IPL1`/`IPH1` bit 2 via the pair-1 arm (§2.6); `enabled()` is overridden because the base form ANDs the whole mask, pair flag included, with `IEN1` — a leg writes `IEN1,#0x84` and fails if the override is removed |
| `CKCON1.SPIX2` | Table 7-2 p.20 | **MODELLED** | validated only while `CKCON0.X2` is set (see §2.2) |
| max slave `SCK` frequency | §16.3.1.2 vs its footnote 3 | **NOT MODELLED — decided** | the two statements are irreconcilable (`FCLK PERIPH/4` vs bus clock) and the pin is asynchronous, sampled once per machine cycle; recorded in `rd2_spiccl.h` |
| electrical Hi-Z on `MISO`, a transmit buffer | §16.2.4, §16.3 | **NOT MODELLED** | a latch cell has no third state, and the part has no transmit buffer to model |
| `SPSTA`/`SPDAT` reset-value contradiction | Table 3-11 `0000 0000b` vs Table 16-4 `00X0 XXXXb` | **kept as found** (§3.2) | `clear_sfr()` answers 0, which satisfies both readings; no behaviour depends on resolving it |

### 2.4 Timers and PCA

| feature | doc4235 | status | evidence |
|---|---|---|---|
| T0/T1 modes 0-2, gate/C-T | §5,§6 | **MODELLED** | `hwtest.py` |
| T2 capture / auto-reload / baud / `EXF2` | §12 | **MODELLED** | baud path fixed locally (`do_t2_baud`'s `*= 6` gated to 1T parts + `serial.cc` `_divby` 16→32, together the 12× that read 8 MC/bit); `while (cycles--)` in `do_t2_reload`/`do_t2_capture` only matters under `T2X2`, see §1.1; `hwtest.py` |
| T2 down counter | §12 | enabled | `t2_default\|t2_down` (`uc52.cc:64`) |
| T2 clock output on P1.0 (`T2MOD.T2OE`) | §12, Table 12-2 p.34 | **MODELLED** | `uc89c51r.cc:85` adds the `t2_clock_out` feature this part documents — precisely the flag `timer2.cc:128`/`:146` gate on — so `T2OE` beats the `T2CON` modes and `P1.0` toggles off the `TL2` path: measured 16 MC low, 16 MC high, 32 MC period, 4 oscillator clocks per `TL2` count, `CKRL` multiplying only the clock cost. `t/t2oetest.py` 9/9 reads the wave from the P1 **latch** (a port-bit read answers 0), `t/ckrltest.py` pins the period in both clock states. Logged here as **ABSENT (inert bit)** until 2026-11-13 — see the superseded D7 |
| PCA: 5 modules, capture / MAT / TOG / 8-bit PWM | §13, Tables 3-6, 13-1 | **MODELLED** | `pca.cc` (`bmPWM` at 243/307), PWM polarity fixed in this fork |
| PCA CEX0-4/ECI edges from **P1** | §13 | **MODELLED** | `cl_pca::happen()` on `EV_PORT_CHANGED`, `ep->id == 1` (`pca.cc:330-354`) |
| PCA clock select `CPS1:CPS0` | Table 13-1, pp. 36-37 | **MODELLED**, correct 2-bit width (RD2 has no `CPS2`) | the encoding is printed on the *continuation* page p.37: `00` FCLK_PERIPH/6, `01` FCLK_PERIPH/2, `10` T0 overflow, `11` external clock at ECI/P1.2. `pca.cc:210-228` yields 1 : 3 counts per machine cycle = FCLK_PERIPH/6 : /2 in Standard mode |
| ECI input rate limit | Table 13-1 p.37 | **ABSENT** | documented max ECI rate = FCLK_PERIPH/4, unenforced: the model accepts edges silicon would miss |
| `ECOM` write side-effects (CCAPnL clears, CCAPnH sets) | §13.2 p.36, §13.3 p.37 | **MODELLED** | `pca.cc:165-176` gates them on `CCAPMn & (MAT\|TOG)`, which is the datasheet's exact scope; §13.4 PWM has no such rule (duty updates by reloading CCAPLn from CCAPnH on CL overflow FF→00). **Do not "fix" this gate** |
| `CMOD.CIDL` stop-in-idle | §13 | **MODELLED** | `pca.cc:206-208` |
| 9th PWM duty bit (`EPCnL`) | — | **not applicable** | RD2 has no `PCA_PWMn` (Table 3-11 lists only CCON/CMOD/CCAPM0-4/CL/CH/CCAP0-4L/H); the `pca.cc:312` note is about other parts |

### 2.5 Watchdog, power, reset

| feature | doc4235 | status | evidence |
|---|---|---|---|
| WDT refresh sequence (`WDTRST` ← `1Eh` then `E1h`) | §19 | **MODELLED** | `wdt.cc:63-64` |
| WDT period from `WDTPRG` `A7h` | §19, Table 19-2 | **MODELLED** | `wdt.cc:76` reads `sfr->get(WDTPRG) & 0x07` and times `(2^(14+S) − 1)` **machine cycles**, so the period is no longer the constructor constant (`uc89c51r.cc:88` still passes `0x3fff`, now as the `S=0` base); `uc89c51r.cc:391` resets `WDTPRG` to `S=000`. `t/wdttest.py` 14/14 pins every `S`, and `CKRL` scales the watchdog in time only, since §19 states the period in machine cycles |
| Idle / Power-down + wake | §18, `PCON.PD/IDL` | **MODELLED** generically | `cl_51core::idle_pd()` (`uc51.cc:2159-2182`), gated on `CPU_CMOS`, which this part has |
| `PCON.POF` power-off flag | §21 Table 21-1 (printed p.87) + §6.1 Table 6-2 (p.15) + family map Table 3-11 (p.7), bit 4 | **MODELLED** (2026-11-13) | `sfr->write(PCON, bmPOF)` in `cl_uc89c51rd2::clear_sfr()` — reset `00X1 0000b`, so POF is 1 out of reset and is a plain read/write bit afterwards ("cleared by software to recognize the next reset type"), hence no hook. Kept out of the family `sfr->write(PCON, 0)` so `ident89c51r` stays byte-identical. `rd2registers` pins the reset value with bit 5 masked out (Reserved, reads indeterminate) |
| POR/PFD supply monitor | §11 | **ABSENT** (out of scope, §7) | — |
| 8-bit clock prescaler for power saving | §6.1/§6.2.1, `CKRL` (§18 is Idle/Power-down and never names `CKRL`) | **MODELLED** (2026-11-13) | §2.2 row below and Phase 6 item 18; `t/ckrltest.py` 35/35 |
| ONCE mode | §20 (printed p.86) | **NOT MODELLED — no register exists** | pin-state entry (ALE low through reset release), nothing to trap; Phase 6 item 19 |
| Reduced EMI mode | §22 (printed p.88) | **NOT MODELLED — decided** | it is `AUXR.AO`, an output-only effect; Phase 6 item 20 |

### 2.6 Interrupt system — the part that had to be fixed first (done 2026-11-13)

Table 17-2 (p72) is authoritative:

| # | polling | source | request | vector | enable | priority bits |
|---|---|---|---|---|---|---|
| 1 | 1 | INT0 | `IE0` | `0003h` | `IEN0.EX0` | `PX0L`/`PX0H` |
| 2 | 2 | Timer 0 | `TF0` | `000Bh` | `IEN0.ET0` | `PT0L`/`PT0H` |
| 3 | 3 | INT1 | `IE1` | `0013h` | `IEN0.EX1` | `PX1L`/`PX1H` |
| 4 | 4 | Timer 1 | `TF1` | `001Bh` | `IEN0.ET1` | `PT1L`/`PT1H` |
| 5 | **6** | UART | `RI+TI` | `0023h` | `IEN0.ES` | `PSL`/`PSH` |
| 6 | 7 | Timer 2 | `TF2+EXF2` | `002Bh` | `IEN0.ET2` | `PT2L`/`PT2H` |
| 7 | **5** | PCA | `CF + CCFn` | **`0033h`** | `IEN0.EC` (bit 6) | `PPCL`/`PPCH` |
| 8 | 8 | **Keyboard** | `KBDIT` | **`003Bh`** | `IEN1.KBD` (bit 0) | `KBDL`/`KBDH` |
| 9 | 9 | *(reserved)* | — | `0043h` | — | `TWIL`/`TWIH` exist in `IPL1`/`IPH1` |
| 10 | 10 | **SPI** | `SPIIT` | **`004Bh`** | `IEN1.ESPI` (bit 2) | `SPIL`/`SPIH` |

Registers: `IEN0` `A8h` (named `IE`), `IPL0` `B8h` (named `IP`), `IPH0` `B7h`
(named `IPH`) — all present. `IEN1` `B1h`, `IPL1` `B2h`, `IPH1` `B3h` —
**MODELLED since 2026-11-13.** `IEN1` `B1h`, `IPL1` `B2h`, `IPH1` `B3h` are named, reset and live,
and `cl_uc::priority_of()` takes the pair selector from the source's `ie_mask` via `IP_PAIR1`
(`regs51.h`, stripped from the register mask — see Phase 3). The keyboard (rank 8, `003Bh`, bit 0) is
the first source through that arm; SPI (rank 10, `004Bh`, bit 2) inherits it. `t/irctest.py` (50
checks) arbitrates a pair-1 source against the first-pair `PT1`, and the ablation in `t/kbd-report.md`
§8 shows exactly which two legs die if the strip is removed.

### 2.7 Flash, boot loader, IAP

Everything below is **ABSENT**. Mechanism, from §24.10 (p111), which is small
enough to state exactly:

- Select the API by loading **`R1`** = command, **`ACC`**, **`DPTR0`**, **`DPTR1`**
  per Table 24-10 (e.g. `R1=00h` + `DPTR0=0000h` → `ACC` = manufacturer ID;
  `R1=01h` + `DPH` ∈ {`00h`,`20h`,`40h`,…} → erase that block).
- Invoke with **`LCALL 0xFFF0`** (`USER_CALL`) — must be `LCALL` so `RETI`… i.e.
  `RET` returns to the application.
- `AUXR1.ENBOOT` (bit 5) must be set first; it also maps the boot ROM.
- The boot loader does **not** disable interrupts; firmware must.

Note `ENBOOT` shares `AUXR1` with `DPS` (bit 0) — the same register the DPTR
banker already watches with mask `1`, so widening that mask or adding a second
decoder is a direct interaction to test.

### 2.8 ED2 only

`EECON` `D2h` (`EEE`, `EEBUSY`), 2048 B EEPROM — **modelled 2026-11-14** (`rd2_eeprom.cc`,
`RD2_EE_ID 17`, Phase 7 item 21). The device identity it needed is the device-row name
(`-t 89C51ED2`), gated by `rd2_is_ed2()`; the two names still share one CPU flag, and only this
feature is split.

### 2.9 Off-chip mechanics — motor, gear train, tach wheel

`rd2_motor.cc` / `rd2_motorcl.h` — **modelled 2026-11-14** (`motor[20]`, reused `HW_TIMER`
category, Phase 8 item 23). Not silicon: a brushed-DC plant, a gear train with backlash and a
quadrature tach wheel, as board parts. There is **no SFR and no register** — the plant has no
addressable side at all. Its inputs are the PWM the MCU already drives (the `pwm_bit` port
latch, which is what `pca.cc:297-307` writes) and its output is pin edges on the tach port,
injected through `cl_port::set_pin` so the port itself raises `EV_PORT_CHANGED` and the timer
counter sees them like any other input. What firmware can see is therefore exactly what a
sensor gives it: edges. `conf` lists it on `89C51RD2` and `89C51ED2` (21/22 elements) and on
nothing else — `89C51R`, `251`, `51R` and `52` do not get it, and item 23 explains why, since
the reason is a core behaviour worth knowing before adding any peripheral.

---

## 3. Defects found during this audit

Fix these before adding features; two of them will actively fight new work.

**D1 — a spurious interrupt source squats on the keyboard vector.**
`interrupt.cc:86-115` adds `PCA_CCF0`, `PCA_CCF1`, `PCA_CF` **unconditionally**,
with enable = `CCAPMn` bit 0 (`ECCF`) and vector `0x003B`. That block is STC12
logic; on the RD2 `ECCF` does not enable an interrupt on its own (`IEN0.EC`
does), and `0x003B` is the **keyboard** vector. Reproduced:

```
image: MOV CCAPM0,#01 ; SETB CCON.0 ; MOV IE,#80h (EA, EC cleared) ; SJMP $
       0033h -> LJMP h33 (MOV P1,#33h) ; 003Bh -> LJMP h3B (MOV P1,#3Bh)
IE=80h  -> P1 = 0x3B    WRONG: no interrupt may occur with EC cleared
IE=C0h  -> P1 = 0x33    right vector, but only because pca.cc's source wins the scan
```

`pca.cc` is correct (vector `0033h`, enable `IE.EC`) — the fix is to gate the
`interrupt.cc` block to the STC devices, not to touch PCA. Done in Phase 0. Now that the **real**
keyboard occupies `003Bh` (Phase 3), a regression here would arrive as a keyboard firing on a PCA
event. The guard is `t/irctest.py`'s "sources added over the 89C51R" leg, which asserts the exact
*set* of `(vector, name)` pairs the RD2 adds — `(0x3b keyboard)`, `(0x4b spi receiver/error)`,
`(0x4b spi transmitter)` — so anything else appearing on `0x3b` fails it; the two count-shaped legs
this replaced ("rows at 0x3b", "one source added") were deleted when the SPI made a count ambiguous.

**D2 — `CCAPMn` reset value disagrees with the datasheet.**
`uc89c51r.cc:201-205` ORs `bmECOM` (`0x40`, bit 6) into all five mode registers
at reset; Table 3-11 gives `CCAPMn` reset `X000 0000`, i.e. bit 7 indeterminate,
**bit 6 = 0**. With `ECOM` forced on, `pca.cc:287` lets the comparator drive the
CEX bits of P1 (`sfr->set(P1, …)` at 304/316) where the part would leave them as
inputs. These lines are fork-inherited (they appear as context, not `+`, in
`rd2-fixes.patch`). Confirm the intended STC behaviour before removing, since
the fork may depend on it — the fix belongs behind the device gate.

**D3 — WDT period ignores `WDTPRG`.** Fixed `0x3fff` (`uc89c51r.cc:69`,
`wdt.cc` has no `WDTPRG` reader), so watchdog timeout is not programmable.

**D4 — `PCAX2` has no effect on the fast PCA timebase.** `pca.cc:217-218`
(`cycles*3`) bypasses `peripheral_cycles()` while `pca.cc:215` uses it.

**D5 — the Ghidra language mislabels the SPI registers.** doc4235 Table 3-8:
`SPCON C3h`, `SPSTA C4h`, `SPDAT C5h` (the SFR-category list on p4 calls the
middle one `SPSTR` — a datasheet inconsistency worth recording). The shipped
pspec uses `c1`/`c2`/`c3`, and that is what is installed under
`$G/Ghidra/Processors/AT89C51RD2/data/languages/`: a write to real `SPCON` displays as
`SPDAT`, and writes to unmapped `C1`/`C2` display as `SPCON`/`SPSTA`. The same
pspec defines **`PCON1` at `DFh`, which does not exist** — the `D0h-D7h` row of
Table 3-11 is `PSW`(D0), `FCON`(D1), `EECON`(D2) and `DFh` is unused. Verified
against the doc's per-register tables (3-1…3-11); the CCAP/CCAPM entries
(0xDA-DE, 0xEA-EE, 0xFA-FE) are **correct** despite tripping the automated diff.

**Fixed.** `SPCON C3 / SPSTA C4 / SPDAT C5` now, `PCON1` deleted, and the same pass
cleared two more phantoms off the bit symbols: `FIFLG.0-.7` → `P4.0-.7` (Table 3-4,
`C0h`) and `HIFLG.0-.7` → `P5.0-.7` (`E8h`) — both names score **0 hits in all 137
pages**. Added the enable-register aliases the datasheet spells inconsistently
(`IEN0`/`IE0` at `A8h`, `IE1` at `B1h`); `IE0`/`IE1` are also TCON bit names at another
address, which is harmless because Ghidra's `primary` flag is per-address. A
symbol-by-symbol re-audit against Tables 3-1…3-11 found no further address errors, and
`t/langtest.py` grew from 12 to 22 legs that resolve names *through Ghidra* instead of
grepping the pspec. Its last two gaps are closed as well: `P5` is now a byte symbol at
`E8h` too (Table 3-4 declares it at both `E8h` and `C7h`; both stay primary, since
primacy is per-address) and `B8h` carries `IPL0` — the p.4 / Table 3-3 spelling — as a
non-primary alias of `IP`. **Trap for the next editor:** within one address Ghidra makes
the *first declared* symbol primary, so `IPL0` must stay physically below `IP`;
alphabetically re-sorting that block would silently relabel every `IP` reference in a
listing, and `t/langtest.py:185` exists to fail if that ever happens.

**D6 — README's gap list is wrong.** It puts SPI at `SPCR A1 / SPSTA C4 /
SPDAT C5` (mixing a DS390 grep hit with the doc) and cites a prescaler
"`CKRL`/`CKRM` `0xBA`/`0xBB`" — the RD2 has **`CKRL` at `97h` and no `CKRM` at
all** (string absent from all 137 pages). **Fixed:** README now cites Table 3-8
addresses, `CKRL 97h`, the absence of `CKRM`, `IEN1`/`IPL1`/`IPH1` and `ENBOOT`, and
links here.

**D7 — `T2OE` was a documented bit with no effect. SUPERSEDED, see the correction below.**
doc4235 Table 12-2 (p.34) defines `T2MOD` `C9h` as `- - - - - - T2OE DCEN`, reset `XXXX XX00b`.
`timer2.cc:45-47`
registers `C9h` because `t2_down` is requested, and `timer2.cc:127` parses `T2OE` into
`bit_t2oe` with a `T2MODE_CLKOUT` branch a few lines below — but that branch is gated on
`features & t2_clock_out`, and `uc52.cc:64` passes only `t2_default|t2_down`. So `DCEN`
works and `T2OE` silently does nothing, and P1.0 never becomes a clock output. This is
the *only* clock output in the datasheet: the phrase "clock output" occurs exactly once
in all 137 pages, on p.34.

**D7 is SUPERSEDED — `T2OE` is modelled, and P1.0 really does become a clock output.** The
paragraph above describes the vendored tree and is kept as the reason the bit was logged inert. The
RD2 now calls `add_features(t2_clock_out)` (`uc89c51r.cc:85`), which is exactly the gate at
`timer2.cc:128`/`:146`, so the `T2MODE_CLKOUT` branch fires: `T2OE` wins over the `T2CON` modes
(§12.2 programs the bit before the `TR2` write that starts the timer, which is why
`do_t2_con_write` re-reads `T2MOD` instead of trusting the cached copy), and the measured wave is 16
machine cycles low, 16 high, a 32-cycle period at 4 oscillator clocks per `TL2` count, with `CKRL`
multiplying only its clock cost (`t/t2oetest.py` 9/9, `t/ckrltest.py`). Read `P1.0` out of the P1
latch with `dump sfr[0x90]` — a port *bit* read answers 0 whatever the latch holds, which is what
made this look unmodellable in the first place.

**D11 — FIXED 2026-11-13: the fixed baud rate of mode 2 was 2× too fast on every part in the
tree. The number first logged for this (24×) was itself a unit error.**
`serial.cc:184-216` sets `_divby = _bmSMOD ? 16 : 32` for modes 1/2/3, and `cl_serial::tick`
accumulates `s_tr_tick += cycles * uc->clock_per_cycle()` (`serial.cc:441`) where `cycles` is the
machine-cycle cost of the retired instruction (`core/sim.src/uc.cc:3344` calls
`tick_hw(inst_ticks)`), so the accumulator runs in **oscillator clocks**. `serial.cc:279`'s
`uc->clock_per_cycle() == 1 ? 16 : 32` shows the author knew the units differ between 1-clock and
12-clock parts.
doc4235 gives the fixed rate for this part directly, in the SCON mode table on printed p. 53
(`/tmp/ds/p052.txt`, row `SM0 SM1 = 1 0`): **"9-bit UART FXTAL/64 or FXTAL/32"** — the same table
that states mode 0 as "FXTAL/12 (or FXTAL/6 in mode X2)". `FXTAL` is the crystal, so the bit is
**64 / 32 oscillator clock periods**, and oscillator clocks are exactly what the accumulator
already counts, so the divisor needs no `clock_per_cycle()` factor at all (5.33 / 2.67 machine
cycles on a 12-clock part).
Measured on this tree by the two-run difference method (`/tmp/m2_*.hex`: the same program with
one and then two transmitted frames, breakpoint on the trailing `SJMP`, difference of the
`Simulated N ticks` lines — never an absolute count): **one extra mode-2 `SMOD=0` frame costs 420
clocks**, which is 11 bits at the coded 32-clock divisor plus the `JNB TI`/`CLR TI`/`MOV SBUF`
poll. Against the correct 64 clocks/bit the model is therefore **2× fast** (and the `SMOD` branch
is 16 against 32, also 2×, as `_divby` literally halves).

**Why the earlier 24× must not be repeated.** It came from two mistakes compounding. The
expectation was recalled as "64 / 32 *machine cycles*", which inflates it 12× to "768 / 384 clocks"
and turns a 2× defect into a 24× one; and the datasheet row that would have contradicted it was
missed because the audit grepped `FOSC/` while this table spells the crystal `FXTAL`. Same shape as
D12 — a figure derived by reading rather than measuring — but worse in kind, because a wrong
*expected* value sends whoever fixes it 12× past the right answer while looking rigorous. Rule:
the expectation in a timing leg is as much a measurement claim as the observation, it gets cited to
a page, and the grep that proves something is absent must use every spelling the document uses.

**Fixed.** `serial_bit_cnt()` (`serial.cc:348-364`) now overrides the mode 2 divisor to
`_bmSMOD ? 32 : 64` **oscillator clocks** when `rd2_euart` is set, with no `clock_per_cycle()`
factor, upstream of the `fixed_divby()` X2/SIX2 scaler. Only mode 2: the same table row gives mode
3 "Baud rate variable — determined by Timer 1 or 2 overflow", so the `/16 / /32` there is the
dimensionless overflow divisor and was already right. Placed upstream of `fixed_divby()` on purpose
— the row states the Standard-mode rate, and `SIX2` is documented (p. 19) as the mode **0 and 2**
UART clock choosing 6 instead of 12 periods per bit in X2, which is how mode 0's already-correct
`_divby = 12` becomes `FXTAL/6`. **The last clause was overtaken by Phase 6 on 2026-11-13 and is
now false:** `CKRL` prescales `FCLK_PERIPH` *together with* the machine cycle, so a row written in
`FXTAL` is exactly the case `CKRL` could never reach. The mode-0/2 accumulators now count
`FCLK_PERIPH` periods on the RD2 EUART with the rows entered halved (12→6, 64/32→32/16), and
`fixed_divby()` doubles for `X2|SIX2` instead of halving. The rates at the reset value are
unchanged, which is what `euarttest` and `ident89c51r` still pin.

Measured on this tree, two-run difference (never an absolute count): an extra mode 2 frame costs
**756 clocks** with `SMOD=0` and **420** with `SMOD=1` (11 bits at 64 / 32 plus the 48-clock CPU
cost of `MOV SBUF`/`JNB TI`/`CLR TI` and up to one 24-clock poll of quantisation). Before the fix
the same measurement gave 420 at `SMOD=0`. `t/euarttest.py` now asserts both windows as derived
bounds — `rate()` over 7 frames / 11 bits must land in 64-78 and 32-46 clocks/bit; it measures
**70.91** and **38.18**, and the pre-fix divisor produced 37.0 / 21.0, well clear of both windows.
Verified: `euarttest` **34/34** (its leg 8 control on `/tmp/s51-ref-b` still passes), and the whole
sim floor after the change — `irctest` 37/37, `ident89c51r` 36/36, `rd2registers` 276/276,
`rd2identity` 38/38, `t2oetest` 7/7, `wdttest` 14/14, `x2test` 15/15, `xramtest` 10/10,
`banktest` 3/3, `ccapmrst` 12/12, `dpstest` 2/2, `pcapin` 21/21, `uartmp` 19/0, `hwtest` 8/8,
`runtests` **11/20** — so no other part and no upstream baseline transcript moved.

**D13 — `priority_of()` reads only `IP`, so `IPH0` is inert and 4-level priority does not
exist.** When several sources are pending at once the core asks `cl_uc::priority_of(is->nuof)`
(`core/sim.src/uc.cc:3424`); `cl_51core::priority_of()` (`uc51.cc:2146`) consults `IP` alone.
`IPH0` (0xb7) is therefore named, reset and writable decoration with no effect, and doc4235
Tables 17-1/17-7/17-8 (pp. 73, 78-79) — which give every source a two-bit level, `00` lowest to
`11` highest — cannot be honoured: a source programmed to level 2 or 3 arbitrates as if it were
level 1, so the winner of a simultaneous pair can be simply wrong. Found 2026-11-13 by the step-10
survey, at the same time as the dead `cl_uc89c51r::it_priority` (§6) that was presumably meant to
implement this. **Fixed the same day under step 10.** Two functions in `uc51.cc`: `priority_of()`
(2209-2231) now answers `(h ? 2 : 0) | (l ? 1 : 0)` from the source's own bit in the pair it
belongs to, and `do_interrupt()` (2104-2165) keeps the **highest** eligible level instead of
taking the first eligible source, with ties resolved in `it_sources` order — which is Table 17-2's
polling sequence. **Gate is `CPU_89C51RD2` alone, deliberately:** the AT89C51RB2/RC2 carries
`IPH0` in this model and is almost certainly affected too, but its own datasheet (doc4234) is not
in this workspace, so its level encoding is unverified — do not widen the fix without that
document. Both arms measured: `t/irctest.py` **37/37** with `/tmp/s51-ref-b` at **26/37**, so the
arithmetic, the highest-wins scan, `IPH0`/`IPH1` decoding and the `Pr` column are all real and
none of them exists in the control binary. Ties still go to the earlier-polled source, level 2
preempts level 1, level 3 is never preempted and equal levels never preempt. `-t 89C51R` keeps the
old scan statement for statement, guarded by `t/ident89c51r.py` **36/36 byte-identical** and
`runtests` still **11/20**.

**D14 — CLOSED as "not a defect", but the register is now named: `EECON` (0D2h) is in the family
SFR map while the array behind it is ED2-only.** Two statements in this document had to be squared
against the pages. Table 3-11 (printed p. 7, `/tmp/ds/p006.txt`) gives `EECON D2h "EEPROM Data
Control"` with `EEE`/`EEBUSY` **and a reset value, `xxxx xx00b`** — my first closure of D14 asserted
the opposite ("no chapter-3 row and no reset-value row") and was simply wrong; that table is the
very one the step-9 reset work is driven from, so leaving D2 unnamed made `dump sfr` disagree with
it. The features list (printed p. 1) and the memory map (printed p. 7) both qualify the *array*:
"On-chip 2048 Bytes EEPROM Block for Data Storage **(AT89C51ED2 Only)**", "EEPROM data Memory SFR
**(AT89C51ED2 only)**". Chapter 3 is a family chapter, so it maps the ED2's register; on a plain RD2
there is nothing for `EEE` to remap. Resolution: name it, and show the absence.

*Done.* `EECON 0xd2` in `regs51.h` (with the qualifier in its comment), a `CPU_89C51RD2` row in
`sfr_tab51` (`uc51.cc:225`), `sfr->write(EECON, 0x00)` in `cl_uc89c51rd2::clear_sfr()`
(`uc89c51r.cc:337-343`) for the documented `xxxx xx00b`, and — the part that keeps the name honest —
a leg in `t/xramtest.py` (`eecon_does_not_remap`, run under `-t 89C51RD2`) proving that `MOVX` at
`0x0100` still reads back the byte written before `EEE` was set, and that `EEBUSY` reads 0 after
`MOV EECON,#0x02`. It is the negative control for the day someone ports Phase 7: the moment an
`EEE` remap reaches the RD2, that leg fails by name instead of silently. `rd2registers` grows to
**284/284** (two rows for the new register), `xramtest` **10/10 boundaries + EECON leg OK**,
`rd2identity` **38/38**, `ident89c51r` **36/36 byte-identical** against `/tmp/s51-ref-b`, full floor
green. The remap itself stays in Phase 7 behind `-t 89C51ED2` — which the tree already accepts as a
device name, so the gate will be one row.

The lesson stands with the sign flipped from my first closure: the whole-document `NAME (0xxh)`
sweep found a register chapter 3's per-register tables omit, and the wrong step was concluding from
one page's "(ED2 Only)" qualifier that the register itself was absent. Absence in a *feature* list
means the side effect is not on this part; it does not mean the address is unmapped. Check the SFR
map and its reset column before either adding a row or refusing to.

**D15 — FIXED 2026-11-14: every counter edge counted twice.** `cl_port::write()` (`port.cc:164-165`,
`:177-178`) and `cl_port::set_pin()` (`:246-247`) refreshed the read-only `port_value` cfg mirror with
`cfg->write()`. `cl_hw::init()` registers every cfg cell back onto its owning element
(`core/sim.src/hw.cc:104`, `cfg->register_hw(a, this, false)`), so that mirror write re-entered
`cl_port::write`; the inner pass skipped the two mirror blocks but still ran `conf()`/`conf_op()` and
reached `inform_partners(EV_PORT_CHANGED)` at `port.cc:184-189`. One latch write, **two** events, and
`T_edge` reached 2 — so timer 0, timer 1, timer 2 (counter *and* capture mode) and the PCA external
clock input all advanced twice per edge. Reproduced on `-t 89C51RD2`, `-t C25`… `-t C52` and
`/tmp/s51-ref-b` alike, i.e. pre-existing and device-independent, and all three sites are
byte-identical in upstream v0.9.12. Fixed by updating the mirror with `cfg->set()` — which stores the
same value without notifying (`mem.cc:1262`, and `CELL_READ_ONLY` is only ever set on ROM ranges in
`f8`/`stm8`, so nothing is refused) — exactly as `cl_port::init()` already did at `port.cc:105`.
Pin-driven edges were never doubled: `set_pin()` informs *before* the mirror write, and in the
re-entrant pass `ep.pins == ep.new_pins`, so the emit guard fails. A timer-side dedupe was rejected
because both events carry identical payloads. `t/t2cnttest.py` (30 legs; 9/30 before, the 9 being the
negative and invariant legs) is the guard, and the fix is upstream-candidate material alongside the
timer-2 one (`~/s51/upstream/`).

**D16 — the PCA run bit sits on the wrong bit address, so `SETB CR` does nothing.**
`pca.cc:80-81` registers the `CR` bit cell at **`0xda`**. `CCON` (`D8h`) is bit-addressable with
`CF CR - CCF4 CCF3 CCF2 CCF1 CCF0` (Table 13-2, `doc4235-spec.md:130`), and `:141` repeats
"`CR` (CCON.6) run control: set/cleared only by software" — so `CR` is `D8h+6` = **`0xde`**, and
`0xda` is `CCON.2` = **`CCF2`**. Measured on `-t 89C51R`, one 2-byte image per row, `step 5000`,
then `dump sfr 0xd8 0xd9` and `dump sfr 0xe9 0xe9`:

```
MOV  CCON,#0x40   ->  CCON=0x40  CL=0x10   PCA runs            (byte write, legal)
SETB 0xde         ->  CCON=0x40  CL=0x00   PCA does NOT run    (CR: byte cell set, pca.cc untold)
SETB 0xda         ->  CCON=0x04  CL=0x0f   PCA runs WRONGLY    (that bit address is CCF2)
```

Two distinct faults from the one address. (a) Datasheet-correct firmware — `SETB CR` / `CLR CR` —
starts and stops nothing, which is what made the plant model look dead: its PCA PWM never ran, so
the duty meter read 0 % and the wheel never turned. (b) Bit access to **`CCF2`**, which firmware
legitimately `CLR`s after a module-2 capture, toggles the counter's run control instead, so a
capture-flag clear silently stops the whole PCA. The name table is right (`uc51.cc:468` maps `0xdf`
to `CF` = CCON.7), which pins this to `pca.cc` rather than to a general bit-space off-by-N. Same
class of mistake as D15: a hook registered on the address of a *sibling* bit.

Fix: register the cell at `0xde` in `cl_pca::init()` and check the `CF`/`CCFn` bit hooks against the
same table while there. Guard: `t/motortest.py` carries a leg that asserts today's broken pair
(bit write inert, byte write live) and is written to **fail loudly with a "delete me" message once
`pca.cc` is fixed**, so the defect can be neither silently re-introduced nor silently forgotten.
Until then the motor suite drives the PCA with the byte form `MOV CCON,#0x40`, which is legal on
the real part, and says so in a comment.

**D17 — FIXED 2026-11-15: setting `AUXR.AO` silenced the UART on the RD2.** `serial.cc:686` read
bit 0 of `AUXR` (`8Eh`) as the STC `S1BRS`/`S1ST2` baud-clock select on every part that reached
it, because the test was "is there an `AUXR` cell" rather than "is this an STC part". On the
AT89C51RD2 that bit is `AUXR.AO` — ALE/EMI control (doc4235 §9, `doc4235-spec.md:499`) — and
firmware sets it for ALE reasons that have nothing to do with the port. Measured: with `AO=1`
timer 1 stopped supplying overflows to the baud path, `TI` never set, and a transmit loop hung
having emitted nothing. Fix: take the STC reading only from parts that carry the bit —
`(uc->type->type & CPU_STC12) ? s->get(AUXR) : 0` — the same mask the rest of the STC-specific
code uses (`interrupt.cc:99`); every STC part in this fork sets `CPU_STC12` (`globals.cc:408-417`)
and no Atmel part does. Gate: leg 9 of `t/euarttest.py` (`leg_baud_source_isolation`), whose
control pair differs *only* in that bit (`ORL AUXR,#0` against `ORL AUXR,#1`, same instruction
length) and whose sensitivity row switches the timer-2 baud source off and must stop transmitting;
the pre-fix binary emits nothing where the fixed one emits `0x30`.

**D18 — FIXED 2026-11-15: leaving clock-out mode left timer 2 at a third of its rate.** The
`T2MOD` write path set the internal mode to `T2MODE_CLKOUT` when `T2OE` was written and never gave
the mode back when `T2OE` was cleared, so a timer 2 that had *ever* been a clock output kept
advancing `TL2` 3 counts per machine cycle instead of 1 — as a baud-rate timer, in capture mode,
whatever the mode bits now said. Fix: the mode comes from `T2CON` through a new
`mode_from_tcon()` helper (the duplicated if-chain in the `T2CON` path, including its dead
`T2MODE_OFF` branch, now calls the same function), and the `T2MOD` path re-derives the mode before
honouring a `DCEN` write. Other parts are untouched by construction: `t2_clock_out` is added only
by `uc89c51r.cc:96` and the STC15 subclass does not override `write()`. Gate: `t/t2oetest.py`
gained a "leaving clock out" section that measures the **rate** as `TL2` counts per machine cycle —
2.71 against an expected 3 while `T2OE` stays set, 1.09 against 1 after clearing it. A pin-level
check was tried first and is insensitive here (even the toggle count reads `0xff`), which is why
the gate is a rate and not a waveform.

**D19 — FIXED 2026-11-15: timer 2's clock output also drove the baud clock.**
`do_t2_clock_out()` publishes `EV_OVERFLOW` on every tick (`timer2.cc:380`) and `cl_serial`
counted that event as a baud tick whatever timer 2 was doing: with `T2OE` set and `TCLK` clear —
clock out on `P1.0`, baud from timer 1 — the UART ran at the clock-out rate and emitted a second
byte. Fix: the serial `EV_OVERFLOW` branch ignores the event unless timer 2 is actually the baud
source (`if (!t2_baud) break;`), `t2_baud` now tracks the real mode (the `EV_T2_MODE_CHANGED`
payload masks `RCLK|TCLK` out unless the mode is `T2MODE_BAUDRATE`, on both write paths), and
`cl_serial::reset()` clears it because `clear_sfr()` zeroes `T2CON` without notifying anyone. The
only other consumer of that event is `pca.cc`, which listens on `id == 0`. Gate: leg 9 again,
sensitivity direction — the pre-fix binary emits `0x30 0x31` where the fixed one emits `0x30`.

### 3.1 Ruled out while auditing the datasheet — do not "fix" these

Four things look like defects against a secondary reading and are not. Each was
re-checked against the page text; `doc4235-spec.md` §3 got the first two wrong and has
been sent corrections.

- **"Timer 1 as a baud source is ~1300× wrong under X2" (this was D12).** The number was real
  and the model was never wrong: `t/euarttest.py`'s `tx_prog(timer1=True)` set `TH1`, `TL1` and
  `TR1` but never `TMOD`, so timer 1 ran in **mode 0 (13-bit)**, whose overflow period is ~8192
  counts instead of the 3 that leg's own docstring assumed. `786432` is precisely
  `32 overflows × 8192 counts × 6 clocks / 2 (SMOD1)` — a 13-bit timer answering a mode-1
  question under X2. Measured with `TMOD=0x20` on three combinations (merged build × `89C51RD2`,
  merged × `89C51R`, pre-integration build × `89C51R`) timer 1 is exact: overflow period
  **36.00 / 18.00 / 36.00** clocks for Standard / X2 / X2+`T1X2` (expected 3 machine cycles at
  12 / 6 / 12 clocks per cycle) and the mode-1 bit **1155.60 / 577.80 / 1152.00** against
  1152 / 576 / 1152, the 3.6-clock excess being the 2-machine-cycle `JNB TI` poll. So
  `cl_uc89c51r::peripheral_cycles()` and the timer-1 → `cl_serial` overflow path are correct,
  `T1X2` included, and `/tmp/d12.py` reproduces the table. `tx_prog` now writes `TMOD`, leg 3
  asserts 288 clocks ±3 % instead of printing, and the suite was 32 checks then (34 now).
  **Why this stays in the document:** an unasserted number in a `print` is not evidence. It
  became a logged defect, a line in another agent's report, and a paragraph in this plan before
  anyone measured it. Any figure that gets cited must be asserted in the suite that produced it.
- **The `ECOM` write side-effect gate.** `pca.cc:165-176` applies "CCAPnL clears ECOM,
  CCAPnH sets ECOM" only when `CCAPMn & (MAT|TOG)`. That is exactly the datasheet's
  scope: the rule is stated in §13.2 compare (p.36) and §13.3 high-speed output (p.37)
  and *not* in §13.4 PWM, where duty updates by reloading CCAPLn from CCAPnH on CL
  overflow FF→00 instead.
- **The PCA `CPS` encoding.** Printed — on Table 13-1's continuation page p.37:
  `00` FCLK_PERIPH/6, `01` FCLK_PERIPH/2, `10` T0 overflow, `11` ECI/P1.2 (max rate
  FCLK_PERIPH/4). Nothing here is guesswork; only the ECI rate limit is unenforced.
- **The two watchdogs.** The hardware WDT drives the RST pin; the PCA watchdog (§13.5,
  module 4 + `CMOD.WDTE`) generates an internal reset that does **not** drive RST, and
  there is no window register at all. D3's `WDTPRG` fix must keep them separate.

### 3.2 Datasheet contradictions and voids

These cannot be resolved from doc4235K; each needs a recorded choice, not research.

- `KBF`: the register text (p.60) says read-only and "all flags are automatically
  cleared by reading the register", the per-bit text says "must be cleared by software".
  Opposite simulator behaviour. **Choose read-clear** — it is the register-level
  statement, and firmware like `if (KBF & 1)` depends on it.
- Reset values given twice with different answers: `CCAPnH` (Table 3-11 `XXXX` vs
  Table 13-5 `0000 0000b`), `SPSTA` (Table 3-11 `0000 0000b` vs Table 16-4
  `00X0 XXXXb`), `SPDAT` (`XXXX` vs "Indeterminate"). Pick the peripheral table, say so
  in the reset comment, and pin it with a test.
- `FCLK_PERIPH` as used by SPI (§16) and the `TnX2` story (§7) are unreconciled; §16's
  table wins for SPI.
- Phantom registers cited in prose: `KBIE` (the register is `KBE`) and `TWIL`/`TWIH`
  (no I2C on this part). Any "clear KBIE" style firmware idiom is a datasheet bug, not a
  model gap.
- `AUXR1` bit 2 reads 0 and is not writable; `WDTPRG` period units are misprinted
  (ms vs s). The 12 remaining voids are listed in `doc4235-spec.md`.

### 3.3 External review of 2026-11-15 (`~/s51/RD2-REVIEW.md`) — verdicts

Eleven items, each verified against the tree or a probe before anything was changed. Two of the
review's own claims did not survive that check and one of its diagnoses was backwards; all three
are recorded here rather than dropped quietly.

- **`SPCON` reset comment — real, comment only.** Table 16-3 (printed p.69) orders the bits
  `SPR2 SPEN SSDIS MSTR CPOL CPHA SPR1 SPR0`, so the reset value `0x14` is MSTR *and* CPHA set
  with **SSDIS clear** — not "SSDIS=1, CPHA=1", which both `uc89c51r.cc` and
  `doc4235-spec.md:391` asserted. The model had it right all along (a cold `info hw spi` prints
  `SPCON=0x14 … SSDIS=0 MSTR=1`), which is what made this documentation rather than behaviour.
  Both texts corrected; the datasheet gloss now cites the bit order that refutes the old reading.
- **`CKRL` comment — stale.** It still claimed the prescaler divider was unmodelled; it is
  modelled (`cl_uc89c51r::ckrl_divider()`, pinned by `t/ckrltest.py` 35/35). Rewritten to point
  at the divider and its gate.
- **Duplicate `BRL`/`BDRCON` bindings — half right, direction reversed.** The duplication is
  real (`sfr_tab51` rows alongside `cl_rd2_brg`'s own bindings) and `cl_var_list::add()` does
  silently delete a same-name var (`var.cc:360-372`), but `cl_51core::init()` builds the
  peripherals (`uc51.cc:986`) *before* `make_vars()` (`:991`), so the table rows were clobbering
  the BRG's descriptions, not the reverse. The two rows are gone and the reason is written where
  each reader would look (`uc51.cc`, `rd2_brg.cc`); `info var BRL` prints its description now.
- **Boot-ROM counters survived `reset` — real.** `cl_rd2_boot::reset()` re-activated the ENBOOT
  bank and left `n_calls` / `n_refused` / `n_written` / `n_erased` / `last_cmd` / `last_result`
  describing the previous run, while every other element zeroes its own (`cl_rd2_spi::reset()`
  zeroes `n_modf`). `USER_CALL=` and the array still survive on purpose: one is the fixed entry
  address from table 24-10, the other is flash. Gate: `t/bootromtest.py` **70/70** with two new
  legs; the pre-fix binary reads `calls=1 bytes_written=1` *after* the reset.
- **`MODF` did not re-arm — real.** `do_modf()` clears `SPEN`, so restoring `SPEN` under a still
  low `SS` is a fresh fault, not a repeat. The guard tested the flag that had just been raised, so
  a routine that wrote `SPCON` *without* the documented `SPSTA` read cleared the fault without
  clearing the condition and the byte shifted anyway (`SPIF=1 bytes=1`, `MODF=1`, `SS=0`) —
  probed end to end, not inferred. Guard removed; `t/spitest.py` **58/58** with a two-variant leg:
  `rearm-clear` (the documented sequence, which already behaved) and `rearm-dirty` (the
  discriminator, which fails on the pre-fix binary).
- **`st.unusable` counted but never printed — real.** `read_sym_file()` tallied the names this
  console can never type — as51 lists bit names like `ACC.0` in the same table as byte names —
  and reported every other dropped class but that one. Now reported in the same block; the suite
  gained `t/symtest_bits.sym` (a real table carrying `ACC.0`/`TCON.6`/`TR0`) and a leg the
  pre-fix binary fails.
- **`.sym` autoload needed the extension-less name — real.** The sibling `.cdb`/`.map`/`.sym`
  search was guarded by `strcmp(nam, f->get_fname()) != 0`, so it ran only when the loader had
  *guessed* the extension: `file prog` bound the symbols, `file prog.ihx` silently did not. The
  guard is gone; the stem is the typed name with its own extension dropped, so both spellings
  behave alike. `t/symtest.py` pins both (16/16) and the exact-name leg fails pre-fix.
- **Motor: no backlash on the first move after power-on — true, by design, undocumented.**
  `slack` resets to 0, so the model starts wound against the flank it is about to be driven off.
  A gearbox parked inside its play would give that play up whichever way it is first driven, and
  nothing on the part records which way that was; zero is the one starting point that makes the
  first measured move reproducible. Documented at the reset site, not changed.
- **Motor: "a real 8051 counts FALLING" — code half right, datasheet half unowned.** Both edges
  are emitted, which is exactly what makes a counter's polarity not matter; but
  `doc4235-spec.md` says nowhere which flank the counter gate answers, and this plan already
  lists t0/t1 versus timer-2 polarity as open. The header now states the question as open here
  instead of asserting an answer it cannot cite.
- **`floor.sh` process hygiene — real symptom, pre-existing cause.** One console reparented to
  PID 1 by an earlier session made every floor report `process-leak(1)`, because the check was
  absolute. It now snapshots the reparented set before the suites and counts only what appears
  during the run, listing pre-existing ones as a warning ("already reparented (not counted)").
  Tested both directions: a decoy planted before the run is reported and not counted, a decoy
  planted mid-run is counted and the floor exits non-zero.
- **Two claims that did not survive checking.** `bpcmdtest`/`symtest`/`maptest` dying with *No
  module named simproc* was a gap in the development mirror, which lacked `t/simproc.py`; a clean
  clone of the published tree passes all three. And `motortest` is not "not counted as a gate" —
  it is in the `SUITES` list of `t/floor.sh` and counted with the rest.

---

## 4. Plan

Ordering rule: fixes → identity → complete the peripherals that already exist →
new peripherals → the flash subsystem. Each step ends with a test in `t/`, and
each is independently shippable.

### Phase 0 — defects and honesty (no new capability)

**Complete.** Each fix is pinned by a test that passes on the current build and fails on
the pre-fix snapshot, so none of them can silently regress. Suites: `langtest` 22/22,
`irctest` 8/8, `ccapmrst` 12/12, `wdttest` 14/14, `t2oetest` 7/7, `x2test` **15**/15,
`rmwtest` 10/10, `dpstest` 2/2, `hwtest` 8/8, `xramtest` 10/10, `banktest` 3/3;
`runtests` 11/20 and `xcompare` 61/104 stay partial by design (§5).

1. **D1 — done.** `interrupt.cc` returns before the STC PCA block unless the part is
   `CPU_STC12`, so the three STC sources no longer squat on `003Bh` (this part's keyboard
   vector). The early `return` ends that function, so nothing else is skipped. *Test:*
   `t/irctest.py` counts the installed sources (`89C51R` 14, was 17; rows at `0x3B` now 0,
   at `0x33` 6; `STC12` keeps its 3 at `0x3B`) and runs the behavioural pair: `IE=80h`
   leaves `P1=0x11` with `CCON=0x41` still pending, `IE=C0h` vectors to `0x0033`. Pre-fix,
   `IE=80h` gave `P1=0x3B`. Side effect: `-t 251`/`-t C251` also lose the 3 bogus rows.
2. **D2 — done.** The set-`ECOM`-at-reset block is gated on `!rd2_mem()`, so the derived
   `cl_uc251` keeps its old behaviour byte for byte. *Test:* `t/ccapmrst.py` reads
   `CCAPM0-4` after reset (all zero) and masks `CCON`/`CMOD` against Table 3-11, proving
   the reset bit was doing damage: `P1` stays `0xff` until `ECOM` is set (`0xff` vs
   `0xf7`, one bit apart). Pre-fix all five read `0x40`.
3. **D3 — done.** `cl_wdt` latches `WDTPRG & 0x07` when the `0x1E,0xE1` sequence enables
   it (`reset_value = (1 << (14+S)) - 1`) and ticks through `peripheral_cycles(…, bmWDX2)`.
   *Test:* `t/wdttest.py` reads `remains` from `info hw wdt` for every S and requires
   `(2^(14+S)-1) - counter` exactly (16381 … 2097149); pre-fix returned 16381 for all
   eight selects.
4. **D4 — done.** Both machine-cycle selects (`CPS=00` and `CPS=01`) now go through
   `peripheral_cycles(cycles, bmPCAX2)`. *Test:* `t/x2test.py` grew 8 → 15 legs; the one
   that bites is `PCA CPS=01 PCAX2 set` — the old bypass lived in the `cycles*3` branch,
   so a `CPS=00`-only row passes even when the bug is present.
5. **D5 — done.** Three SPI addresses fixed, `PCON1` deleted, the enable-register
   aliases added, and every remaining symbol re-checked against Tables 3-1…3-11: no
   further address errors, but two more other-vendor phantoms (`FIFLG`, `HIFLG`) were
   occupying bit symbols that belong to `P4`/`P5`. `t/langtest.py` is now **22 legs** and
   resolves names through Ghidra instead of reading the pspec — verify with
   `./langbuild && python3 t/langtest.py`.
6. **D6 — done.** README's gap list corrected to Table 3-8 addresses and pointed here.
7. **D7 — done.** `t2_clock_out` is added per-part in `cl_uc89c51r::mk_hw_elements()`
   (timer 2 found by `HW_TIMER`/`id == 2`); the shared `uc52.cc:64` mask is untouched.
   `T2MOD.T2OE` (bit1, Table 12-2) overrides the T2CON
   mode, and clock-out increments TL2 at `FCLK_PERIPH/2` (3 counts/MC) toggling P1.0 on
   overflow, which gives the datasheet's `FCLK_PERIPH / (4·(65536−RCAP2))`. *Test:*
   `t/t2oetest.py` measures the step at which P1.0 first goes low and requires P1 to stay
   `0xff` with `T2OE` or `TR2` clear.
8. **D8 — found while proving D4, fixed.** `cl_pca::reset()` (`pca.cc:326-342`) never
   initialised the `clk_source`, `bit_CR`, `CIDL`, `WDTE`, `ECF` and `ccapm[]` caches,
   which only `write()` refreshes: with `CMOD` never written, `clk_source` held allocator
   garbage (usually the Timer-0-overflow clock), so **the PCA counter never ran out of
   reset**, and a garbage `bit_CR` could start it while `CCON.CR` was clear. The caches are
   now initialised from the register *reset constants* rather than read back, because
   `cl_51core::reset` runs the hw elements **before** `clear_sfr`. Same root cause as the
   `sfr->set()` / `sfr->write()` split in item 2: `set()` skips the `register_cell`
   notifications that keep a peripheral's private copy current. *Test:* the
   `PCA counter ran from reset` leg of `t/ccapmrst.py` — `CL` must move with `CMOD`
   untouched.
9. **D9 — found while checking the CEX pin map, fixed.** The masks themselves are
   **correct**: `bmCEX0..4` (`regs51.h:194-198`) are `0x08,0x10,0x20,0x40,0x80` = P1.3…P1.7
   and `bmECI` is P1.2, exactly §13 p.35. What the test found instead was that
   `do_pca_module()` — the only consumer of the CEX edge flags and the only place the
   16-bit comparator is evaluated — ran **only when `CH:CL` overflowed** (old
   `pca.cc:249-257`). Every consequence is silent: capture latches `{CH,CL}` up to 65536
   counts after the edge (never, if the counter does not wrap), `ECOM|MAT` never sets
   `CCFn`, high-speed output never toggles, and the module-4 watchdog cannot fire. The
   comparator is now evaluated for every count of `do_pca_counter()` (guarded by
   `ccapm & bmECOM`, so an idle PCA pays five compares) and capture lives in
   `do_pca_capture()`, called from `happen()` as the port event arrives so the latched
   value is the counter *of that instant*. This also fixes `CAPN` clearing `cex_pos`
   instead of `cex_neg` (old `pca.cc:281`), which left a negative-edge flag armed
   indefinitely once both senses were enabled. *Test:* `t/pcapin.py`, 21 legs; the same
   image scores **11/21 on `/tmp/s51-ref`** and 21/21 after, with `CL` identical (52) in
   both runs, so nothing but the module evaluation moved.
10. **D10 — found with D9, fixed.** The module-4 watchdog branch called `cl_pca::reset()`,
    which only clamps the PCA's own state and returns: the program keeps running, which is
    the opposite of a watchdog. §13.5 p.46 wants an internal reset that deliberately does
    not drive the RST pin, so it now calls `uc->reset()` exactly as `wdt.cc:94` does.
    *Test:* the `G` legs of `t/pcapin.py` — all SFRs blank at step 10, the program has
    re-armed by step 18 (`CCON=0x40`), reset again by step 20; the `H` control runs the same
    image with `WDTE` clear and must merely set `CCF4` (pre-fix it did not even do that).


**Reviewed and deliberately left alone:** the `IP.7` symbol at `bf` and the `F8.0-.7`
placeholders in the pspec (other-vendor scratch, harmless); any symbol for the p. 4 name
`PI2`, which is given no address anywhere in doc4235; and the pre-existing "every 8th bit"
`SFR-BITS` overlay `BUG` comment in `<default_memory_blocks>`.

### Phase 1 — device identity, then the SFR plumbing layer

**Steps 1-6 — done and reproduced independently.** `CPU_89C51RD2 = 0x8000`, and the file
that needed editing is `core/utils.src/stypes.h`, *not* `sims/s51.src/stypes.h`: the 51 block
uses only `0x0001…0x4000` (`CPU_51 … CPU_STC12`), technology flags live in
`cpu_entry.subtype` rather than `type`, and `CPU_MCS48` already reuses the same bit values in
another family, which is the in-tree precedent for the choice. It is in `CPU_ALL_52`
(requirement 2), and `-t 89C51RD2` / `-t 89C51ED2` resolve — `89C51ED2` is a real part,
§23 is its EEPROM, modelled since 2026-11-14 (Phase 7 item 21).

**The trap had a second form, one gate over.** `CPU_ALL_52` membership buys the *standard*
51/52 names, but `AUXR`, `CKCON0`, `AUXR1`, `WDTRST`, `SADDR`, `SADEN`, `IPH`, `CCON`, `CMOD`,
`CCAPMn`, `CL`/`CH`, `CCAPnL/H`, `T2MOD` and the `CCF*/CEX*/EC/PPC` bits are gated on
`CPU_89C51R` in `make_vars()` (`uc51.cc:1331-1343`), so a flag-only RD2 would still have
printed a nameless SFR dump. Hence a third composite, `CPU_ALL_89C51R`, over those 46 rows,
which leaves bare `CPU_89C51RD2` free for registers only the RD2 has (steps 7-8). A probe
build with the flag removed from `CPU_ALL_52` loses **25 of 43** expected names silently — no
error, no warning — which is why `t/rd2identity.py` asserts names and not just values.
`cl_uc89c51rd2` now exists as an empty subclass of `cl_uc89c51r` (`uc89c51rcl.h:67-84`,
dispatched from `sim51.cc`): the identity plus the home for RD2-only `add_hw()` calls.
`rd2_mem()` was **widened, not split** — no feature was re-scoped in this step.
*Evidence:* `t/rd2identity.py` 38/38 (19 legs × both new names; device resolves + a
near-miss-name control, `AUXR`/`CKCON0` reset values *by name*, trace names `AUXR`/`WDTRST`/
`CCON`/`P0`, all 43 names in `dump sfr 0x80 0xff`, the XRS boundary probes, the X2 differencing
pairs) versus 2/38 on `/tmp/s51-ref` — only the two "near-miss name must be rejected" controls
pass there. `-t 89C51R` is sha256-identical to the pre-change binary across five scripts
(reset SFR dump, timer-2 run, 1024-byte XRS image, 5000-step `isa.ihx` with state and memory
dumps, `info hardware|block|cpu` + `var list`); the only raw diffs are ucsim's own
`Host usage:`/`Runtime:` bookkeeping lines.

**The decision that shapes everything else.** RD2-only features are currently
gated on `CPU_89C51R`, which is also the Atmel **89C51RB2/RC2** family — parts
with no SPI, no keyboard, no `ENBOOT`, and a *different* `XRS` size table.
Adding SPI/KB/flash under that flag would silently change behaviour for those
users, and `rd2_mem()` already exists only to keep `cl_uc251` out.

Recommendation, re-checked against the code rather than from memory: add
`CPU_89C51RD2 = 0x8000` (`stypes.h`, the 51-family block ends at `CPU_STC12 =
0x4000`, so `0x8000` is the first free bit; `enum cpu_type` is one *cross-family*
enum — `DEV_STM8L15x46 = 0x10000000` lives in it — so reusing a bit from another
family is harmless because each family has its own device table, but never reuse a
bit inside the 51 block), device entries `89C51RD2` and `89C51ED2` (`globals.cc:380`,
row shape `{"89C51R", CPU_89C51R, CPU_CMOS, "89C51R", "cmos"}`), `case
CPU_89C51RD2:` in `sim51.cc:97` returning a `cl_uc89c51rd2` subclass, and split the
gate:

```
rd2_common()  -> CPU_89C51R | CPU_89C51RD2   // XRAM window, X2, PCA, WDT
rd2_feature() -> CPU_89C51RD2                // SPI, KB, BRG, ENBOOT, POF
```

Two mandatory wiring steps, both easy to miss and both silent when missed:

- **Add the new flag to the `CPU_ALL_52` composite (`stypes.h:142`).** There are
  **72** sites gated on it, all in `uc51.cc`, starting with the SFR name table
  (`uc51.cc:162` `{CPU_ALL_51|CPU_ALL_52|CPU_251, 0x80, "P0"}`). Skip this and the new
  part boots with no SFR names at all — every trace and `dump sfr` prints raw
  addresses — plus whatever else those 72 registrations cover. Nothing errors.
- **Widen `rd2_mem()`, which is the only flag test in the model.** `uc89c51r.cc:95`
  returns `type->type & CPU_89C51R` and has 6 call sites: `clock_per_cycle()` (116),
  `peripheral_cycles()` (127), 149, `decode_xram()` (160), and the reset defaults
  `AUXR=0x08` / `CKCON0=0` (210, 213). Everything else in `cl_uc89c51r` is
  unconditional, so a new flag that isn't ORed into that one predicate yields a device
  with no X2, no `peripheral_cycles`, no 1792 B XRAM window and no RD2 reset values —
  and no diagnostic. It also means the "split" is cheap: widen one predicate, add one
  sibling, put RD2-only `add_hw()` calls in the subclass ctor. The existing comment on
  `rd2_mem()` (cl_uc251 inherits this class and must keep getting `false`) stays valid,
  since `CPU_251` is a different bit.
  *Probe after wiring:* `dump sfr[0x8e]` must read `0x08` on the new `-t` name, and the
  `t/xramtest.py` / `t/x2test.py` legs must pass under `-t 89C51RD2` as well as `-t 89C51R`.

Keep `-t 89C51R` working byte-identically to today, so every existing test in
`t/` is the regression proof for this refactor. This is the one step that is
expensive to defer past Phase 3.

Then, in `cl_uc89c51rd2`:

7. `#define`s in `regs51.h` for `IEN1 B1h`, `IPL1 B2h`, `IPH1 B3h`, `KBLS/KBE/
   KBF 9C-9Eh`, `SPCON C3h`, `SPSTA C4h`, `SPDAT C5h`, `BRL 9Ah`, `BDRCON 9Bh`,
   `CKRL 97h`, `WDTPRG A7h`, `FCON D1h`, `P4 C0h`, `P5 C7h`. Watch two existing
   collisions: `P4`/`P5` are already `0x80`/`0xA1` for DS390 and `0xC0`/`0xA2`
   for STC12 — the name table is mask-gated so this is safe, but the *macros*
   are not, so use distinct names if a clash appears.
8. Name-table entries under `CPU_89C51RD2` for all of the above (pattern
   `{CPU_89C51R, 0x8f, "CKCON0"}`, `uc51.cc:198-200`). This alone makes traces
   and `dump sfr` readable and is the cheapest big win.
9. Per-SFR **reset values** from Table 3-11 (`AUXR1` bit 2 reads 0, `IEN1`
   `XXX0 X000`-style indeterminate bits, `T2MOD XXXX XX00`, `FCON XXXX 0000`,
   `CMOD 00XX X000`, `CCON 00X0 0000`, `CCAPMn X000 0000`).
10. Second priority pair: extend `it_priority()` to take the register-pair
    selector — cleanest is a `cl_it_src` subclass carrying which pair it uses,
    since sources already carry a priority mask. Bit positions are on the record,
    pp. 78-79: `IPL1` = `- - - - - SPIL TWIL KBDL` and `IPH1` likewise, so **KBD is
    bit 0 and SPI is bit 2**; bit 1 is printed `TWIL` although this part has no I2C and
    its own description says Reserved — treat it as reserved, don't wire a source to it.
    *Test:* `t/irctest.py` gains a 4-level × both-pairs matrix, asserting the
    ISR entry order of two simultaneously-pending sources.

**Steps 7-9 — done and reproduced independently.** `t/rd2registers.py`: **276/276** on the new
binary, and the same file scores 128/310 against `/tmp/s51-ref-b` (PCA fixes, pre-Phase-1) and
90/310 against `/tmp/s51-ref`, so the legs discriminate rather than merely pass.
`regs51.h:79-94` carries `CKRL 97`, `BRL 9a`, `BDRCON 9b`, `KBLS/KBE/KBF 9c-9e`, `IEN1 b1`,
`IPL1 b2`, `IPH1 b3`, `RD2_P4 c0`, `SPCON/SPSTA/SPDAT c3-c5`, `RD2_P5 c7`, `RD2_P5_BIT e8`,
`FCON d1`; 17 `sfr_tab51` rows gated on `CPU_89C51RD2` **alone** (`uc51.cc:201-225`) plus 16
`bit_tab51` rows for P4/P5 (`uc51.cc:537-559`); Table 3-11 reset values in
`cl_uc89c51rd2::clear_sfr()` (`uc89c51r.cc:290-339`) written through `write()` so the owning
peripherals' caches (CCON/CMOD/CCAPMn in `cl_pca`, T2MOD.T2OE in `timer2`) cannot disagree with
the register after a reset.

The `P4`/`P5` collision predicted above arrived exactly as described — `P4`=0x80 and `P5`=0xa1
are DS390's and `#define`s are not mask-gated — so the RD2 ports got distinct macro names.
**A second trap surfaced here and is worth repeating:** `cl_var_list::add()` calls `del(name)`
first, so binding a second address under an existing name silently *deletes the first binding*.
`P5` cannot therefore be named at both `0xc7` (byte) and `0xe8` (bit); the bit cell is
`RD2_P5_BIT`, both reset to `0xff`, and writes do not mirror between them — a known modelling
gap, pinned by the suite so a future mirroring fix has to be a deliberate act. Only P4/P5 got
bit rows: `B1-B3`, `9C-9E`, `C3/C4`, `9B` and `D1` are not bit-addressable, so there is no
`ESPI:` or `KBD:` bit name and the suite proves `0xb1` still follows `P3`. Left out with reason:
`PI2` (no address anywhere in the datasheet), `EECON` (**not bit-addressable** — printed p.92; the
array behind it is ED2-only and modelled, §2.8), TWI names
(phantom). SPI addresses came from Table 3-8's own `Add` column, not the p.7 SFR map, whose
columns interleave under `pdftotext` and read as C1-C3.

**The `rd2_mem()` split question is now closed, and the answer is "widen nothing".**
`rd2_mem()` (`uc89c51r.cc:108`) stays the predicate for what the AT89C51RB2/RC2 genuinely shares:
X2 and `peripheral_cycles()`, the 1792-byte XRAM window and `decode_xram()`, the `AUXR=0x08`
reset default, and the `T2MOD.T2OE` feature registration. RD2-only things never consult it —
they are either gated on `CPU_89C51RD2` in the name tables or registered in
`cl_uc89c51rd2::mk_hw_elements()` (the BRG today; keyboard and SPI next). Two features sit in the
wrong camp and are knowingly left there because the datasheet that would settle them (doc4234,
for the RB2/RC2) is not in this workspace: `T2OE` and `IPH0`. Both are modelled or named for the
whole family, so `-t 89C51R` inherits behaviour that is only *documented* for the RD2/ED2. Do not
widen or narrow either one until doc4234 is in hand; if it ever arrives, `T2OE` and `IPH0` are the
two things to check against it, and `t/ident89c51r.py` is the guard that will show what moved.

**Step 10 — 4-level priority. Done, and it moved the target of the brief.** The brief said "give
`it_priority()` the pair selector". `cl_uc89c51r::it_priority` is **dead**: defined
`uc89c51r.cc:358`, declared `uc89c51rcl.h:50`, zero callers in the whole tree — upstream carries a
priority function that has never run, which is how D13 survived review. The live path is
`cl_uc::priority_of()` (`core/sim.src/uc.cc:3424`) reached through `cl_51core::priority_of()`,
together with the winner scan inside `do_interrupt()`. The dead body was left byte-identical on
purpose so the upstream report can show it as found (§6).

Two things for the next person in this code. `IP_PAIR1` (`0x80`) is a local `#define` in `uc51.cc`
with **nothing setting it**, so the pair-1 arm of `priority_of` is unreachable today; it belongs in
`regs51.h` the first time a source declares it, which is the keyboard (bit 0) and SPI (bit 2)
work — carry that into the Phase 3/4 briefs. And `TWIL` (bit 1 of `IPL1`/`IPH1`) is reserved: it
is neither decoded nor given a source, verified by a leg that writes both registers with every bit
set and still finds 14 IRQ rows and nothing at `0x43`. Legs `RD2 pair1 05: no preempt`,
`RD2 pair1 ff: L1 vs L2` and `pair1 ff: levels` pin that the second pair changes nothing for
sources which do not declare `IP_PAIR1`. What stays untestable until KBD/SPI exist: their
arbitration in both directions, poll ranks 8 and 10, and `IEN1` gating.

**Ghidra name-coverage audit (the "pspec vs Tables 3-1..3-11" item) — no gaps, one real defect on
the other side.** Method: every `sfr_tab51` row (`uc51.cc:67-401`) visible to the RD2 view, against
every `<symbol name="…" address="SFR:…"/>` in `at89c51rd2.pspec`. `bit_tab51` (406+) and `BITS:`
symbols are excluded on purpose: both tools model bits in the bit space, and mixing them in makes
the comparison meaningless (it produced 32 phantom "gaps" the first time). Results: all **17**
RD2-only byte registers carry a Ghidra symbol, as do the 54 family rows; `DPL`/`DPH`/`T2MOD` appear
in the pspec but not in `sfr_tab51` because ucsim names those through their hardware cells, which
`dump sfr` confirms. Two naming differences, both resolved in the datasheet's favour: the pspec's
`IPH0` is the RD2 name while ucsim's row is the generic `IPH`; and the pspec declares `P5` at
**both** `SFR:c7` and `SFR:e8`, which looked like the stock union's sloppiness and is not — Table
3-4 reads `P5 E8h 8-bit Port 5` and `P5 C7h 8-bit Port 5 (byte addressable)`. ucsim calls the
second cell `P5_BIT` only because its var list binds one name to one cell, so cross-tool name
comparison maps `P5_BIT` → `P5`; renaming the Ghidra side to match the simulator was attempted,
caught by `t/langtest.py`'s E8/C7 leg, and reverted with that reasoning in a pspec comment. The
audit's second product was a false alarm, corrected in D14: the pspec's `EECON` at `SFR:d2` is an
AT89C51ED2-only register (features list p. 1, memory map p. 7), so ucsim's silence about it under
`-t 89C51RD2` is correct and the pspec is the over-inclusive side.

### Phase 2 — finish the EUART (highest value per risk)

All of it lives in `serial.cc` + the baud path; no new pin model, and the RX/TX
plumbing already works.

11. **BRG**: `BRL` reload + `BDRCON` (`BRR` run gate, `TBCK`/`RBCK` select Tx/Rx
    independently and make `TCLK`/`RCLK` don't-care, `SPD` bypasses a ÷6, `SRC` also
    feeds mode 0). `stc12_brt.cc` is the structural template — same independent
    reloadable baud timer, same modulo-prescaler shape from §1.1.
    Tick math, derived by hand in §5 rather than from §14 (which never states it):
    `FCLK_PERIPH` is `Fosc/2` in Standard and `Fosc` in X2, and the machine cycle is
    `Fosc/12`/`Fosc/6`, so the BRG counts **once per 6 machine cycles in both modes**.
    Express the divisor as `FCLK_PERIPH`-relative so Phase 6's `CKRL` divides PCA and
    BRG together for free. The UART takes the BRG overflow through ÷32 halved by
    `PCON.SMOD1` — *not* the ÷16 of the Timer-2 path, so keep the two paths distinct
    in `_divby`. `SIX2` belongs to this phase and touches only the fixed mode-0 and
    mode-2 rates (p.19).
    *Test:* the datasheet's own baud tables are the vectors (§5): 16.384 MHz with
    `BRL`=247 and 24 MHz with `BRL`=243 must land at −1.23 % and +0.16 % under both
    table configurations; assert `TBCK=1` no longer falls back to timer 1, and assert
    `X2=1`+`SIX2=1` holds mode 0 at `Fosc/12` while `SIX2=0` doubles it.
**Step 11 — done, measured against the datasheet's own worked vectors.** `cl_rd2_brg`
(`rd2_brg.cc` / `rd2_brgcl.h`, `HW_TIMER` id 13, partner `HW_UART/0`, shaped after
`cl_stc12_brt`) plus a per-direction clock select in `serial.cc`. Six of Table 14-5/14-6's
vectors reproduce to the clock — a least-squares fit over all six gives
`T_meas = 1.0000·T_spec + 1.46`, i.e. a constant poll-granularity offset and no slope error; the
16.384 MHz rows sit +1.23 % exactly as the datasheet's own error column says. `BRR=0` stops the
generator outright (no fallback to a timer, byte counts prove nothing leaves), `TBCK=1` holds its
rate with `TR1` both ways, `SPD=1` measures a 6.0× ratio at two different reloads, and `SIX2`
leaves a BRG-driven rate bit-identical (147.00 vs 147.00 on mode 1, 147.75 vs 147.75 on mode 0
with `SRC=1`) while nearly doubling the fixed X2 rates. `t/euarttest.py`: 34 checks now (31 when this was recorded),
and `/tmp/s51-ref` cannot pass them (it has no `brg[]` element and sends nothing with `TBCK=1`
and T1 stopped) — note that against a BRG-less binary the suite *hangs* in the `JNB TI` loops
rather than failing fast, so give it a wall-clock budget when using it as a control.

*Integration decision (mine, differing from the implementation as written):* the BRG is
registered in **`cl_uc89c51rd2::mk_hw_elements()`** rather than behind a `rd2_euart()` predicate
on the family class, because the widest flag the family class answers to is `CPU_89C51R`, which
also covers the AT89C51RB2/RC2 — a part with no BRG. The subclass hook *is* the gate, so
`cl_uc89c51r` gained only `fclk_periph_per_cycle()` (6, and the documented future home of
`CKRL`, which must divide PCA and BRG together in Phase 6). `cl_serial` performs no identity
test at all: it discovers the element in `init()`/`new_hw_added()`, which is why `-t 89C51R`
stays byte-identical (`t/ident89c51r.py` 36/36) and why `BRL`/`BDRCON` get their console names
from the peripheral's own `init()` instead of `sfr_tab51`, for the same reason.

*Corrections to my own brief, all three caught by measurement:* `BDRCON` is
`BRR(4) TBCK(3) RBCK(2) SPD(1) SRC(0)` per Table 14-13 — my brief had every bit shifted up by
three and the datasheet won; `SPD=1` is the FAST setting (it *removes* the ÷6, it does not add
one); and **mode 2 has no BRG path at all** (`SRC` is mode 0 only, `TBCK`/`RBCK` are modes 1/3),
so the "BRG-driven mode 2" acceptance leg was unrealisable and became the SIX2-invariance legs
above. `SIX2` must never reach `peripheral_cycles()` — it scopes the UART divisor, not the CPU
or any other peripheral (`t/x2test.py` 15/15 is the guard that would have caught it).

12. **Multiprocessor and framing**: `SM2` filtering with the match rule
    `(RX9 & SADEN) == (SADDR & SADEN)`, broadcast address `SADDR | SADEN` (reset 0/0 is
    all-don't-care, which is what preserves 80C51 compatibility), `SM2` inert in mode 0,
    mode-1 matching using the stop bit as the 9th bit, and the `SCON.7` mux: with
    `PCON.SMOD0`=1 the bit is `FE`, latching on a bad stop bit, cleared only by software
    or reset, and `RI` moves onto the stop bit.
    *Test:* two frames with the 9th bit set/cleared against a programmed
    `SADDR`/`SADEN` pair; assert RI only on match. Seed two distinguishable
    payloads so the result names which byte arrived. A third frame with a broken stop
    bit must set `FE` and keep it set after a following good frame.
13. Fix `serconf_check_often` (`serial.cc:88-92`) so RX can be driven from
    batch mode — prerequisite for automated UART tests.

**Steps 12 + 13 — done: the receive side of the enhanced UART.** One contract held throughout:
`core/sim.src/serial_hw.cc` is untouched, everything lives in `serial.cc` (+348) and
`serialcl.h` (+37). Three behaviours, all gated on `rd2_euart` so the classic parts keep the old
unconditional path:

* **`uart0_rx9`** (cfg slot 8, `serconf51_rx9`, default 0). Because a byte-per-frame stream cannot
  express a 9th bit or a stop-bit level, with the flag set the async modes pull a control byte
  from the same input stream right behind each data byte (bit0 = 9th bit, bit1 = stop bit; a
  short read is treated as a good frame, i.e. legacy behaviour). Mode 0 is exempt — it has
  neither bit (p. 48: framing detection "is provided for the three asynchronous modes").
* **The `SM2` address filter** in `cl_serial::tick` (`serial.cc:549-589`): a frame is accepted only
  if it is a command frame (9th bit set; in mode 1 the stop bit takes that place, p. 48) *and* the
  payload matches the given address `SADDR & SADEN` or the broadcast `SADDR | SADEN` (pp. 48-49).
  A rejected frame leaves `SBUF`, `RB8`, `RI` and the received report alone, which is what "the CPU
  is not interrupted by command frames addressed to other devices" promises.
* **`SMOD0` → `SCON.7` mux and the `FE` latch** (`serial.cc:211-224`, `252-261`, `541-548`): with
  `SMOD0=1` bit 7 reads as framing error, hardware sets it on a bad stop bit and "subsequently
  received frames with valid stop bits cannot clear FE" (p. 53) — only software or reset does. The
  mux is pushed through `scon_bits[7]` on the transitions so `dump sfr`, the bit space at `0x9f`
  and `MOV A,SCON` agree, and while `SMOD0=1` writes to bit 7 land on the latch, not on `SM0`.

Verified: `t/uartmp.py` **19 checks, 0 failures** (9th-bit and stop-bit framing, the SM2 matrix
over own/broadcast/foreign address and command/data frames, `FE` sticking through good frames);
`/tmp/s51-ref-b` fails it at the first leg because `uart0_rx9` is not even a named variable there,
so the control fails fast rather than passing vacuously. Floor after landing: `irctest` 37/37,
`ident89c51r` 36/36 byte-identical, `rd2registers` 276/276, `rd2identity` 38/38, `euarttest` 32/32
at the time, `runtests` 11/20.

Three traps this step paid for, all in §5 now or here:

* **A `conf_op` handler must never call `cell->set()`.** Reaching `conf_op` already means the cell
  was written; writing it again re-enters the notification forever and the simulator spins at 100 %
  CPU before the first instruction retires. Mirror into a member, or read the cell back — never
  write it (`serial.cc:288-298`).
* **`cl_f` silently drops `0x00`, `ESC` and `0xff` from a `-S in=` stream**, so a control byte for
  `uart0_rx9` must be non-zero in those positions; a test that needs `ctrl = 0x00` reads as a
  missing byte and silently takes the legacy "good frame" branch.
* **`cl_uc51r::received()` (`uc51r.cc:126`) is dead code and disagrees with p. 48** — it must not
  be revived as the place to hang receiver behaviour; the live path is `cl_serial::tick`.


### Phase 3 — keyboard interface (first new peripheral)

> Anything here and in Phase 4 adds a file to `s51.src/` and an `add_hw()` call, so it also needs an
> `objs.mk` entry. It does **not** need a regenerated `test/config/baseline/89C51R`: both blocks are
> gated on `CPU_89C51RD2`, which has no baseline row — see §1.1.


`KBLS` `9C` (level select), `KBE` `9D` (input enable), `KBF` `9E` (flag), pins
**P1.0-P1.7** (shared with PCA CEX — the port-contention rules of §13 apply).
Rules that are easy to get wrong (§15, verified on p.60): detection is a **level**, not
an edge — `KBLS.x`=0 flags a low level on P1.x, 1 a high level; `KBF.x` follows the
programmed level **regardless of `KBE`**; the interrupt request is `KBF.x AND KBE.x`, so
`KBE`=0 masks the request while leaving the pin as ordinary I/O; `KBF` is read-only and
clears entirely **on read** (§3.2); keyboard activity exits both Idle and Power-down.
Work is: a `cl_hw` sampling P1 through the same `EV_PORT_CHANGED` hook `pca.cc:330`
already proves works, plus the new `003Bh` source (Phase 1 items 7-10 make it possible).

*Test:* drive a P1 bit from a test image, assert `KBF` set, ISR taken at `003Bh`, and
that a second read of `9E` returns 0 (read-clear); assert no *request* when `KBE` is
clear even though `KBF` still sets; assert the flag appears for the programmed level and
not the opposite one.

**One interaction neither phase brief mentions, and both now collide on it.** Printed p. 9
(`/tmp/ds/p008.txt:11-15`) gives every keyboard pin an alternate function as well:
`P1.0/T2`, `P1.1/T2EX/SS`, `P1.2/ECI`, `P1.3/CEX0`, `P1.4/CEX1`, `P1.5/CEX2/MISO`,
`P1.6/CEX3/SCK`, `P1.7/CEX4/MOSI`. So the keyboard (all eight), SPI (four), the PCA (five `CEXn`
plus `ECI`) and timer 2 (`T2`/`T2EX`) are consumers of one latch — the SPI master drives exactly the
pin `TEXEN` watches for a timer-2 reload, and the keyboard level-detects on the pins SPI drives as
outputs. The datasheet's own answer is that each block is inert until its own enable is set
(`KBE.x` cleared = "enable standard I/O pin", p.61; `SPEN`=0; `ECOM` clear in `CCAPMn`, p.8), so the
rule for this project is: **a block may only write a P1 pin when its own enable is set, and every
pin write is a read-modify-write of the latch.** Each of the three suites must therefore leave the
*other* consumers alone — a leg that runs SPI while timer 2 has `TEXEN` set, and a leg that reads
back the four P1 bits the block does not own. Anything else means enabling one peripheral silently
corrupts another, which is the D1 defect class wearing a different hat.

**Landed 2026-11-13** (`t/kbd-report.md` is the worker report; `rd2_kbd.cc` + `rd2_kbdcl.h`,
`t/kbdtest.py` 30 checks — 30/30 on the merged build, 1 PASS / 29 FAIL in ~1 s against
`/tmp/s51-ref-b`, the lone control pass being the `89C51R` absence guard). Decisions worth
re-deriving:

- Sampled **once per machine cycle**, not from the `EV_PORT_CHANGED` hook this phase first proposed:
  the comparator's answer also changes on a `KBLS` write and on leaving reset with `P1` = `0xff`, and
  a port event reports neither. `CKCON0`/`CKCON1` have no keyboard clock-hold bit (`WDX2`/`PCAX2`/
  `UARX2`/timer bits only), so an asynchronous comparator is the documented behaviour, not an
  assumption. `do_inst()` ticks hardware before `do_interrupt()`, so a `MOV P1,#..` that presses a
  key is served at the same instruction boundary.
- **Level, not edge.** A held key makes two consecutive `KBF` reads both return the flag (`D2`); a
  released one gives `01 00` (`D1`). Reason: printed p. 82 lists the keyboard beside INT0#/INT1# as a
  power-down exit with "same behavior as external interrupt", and those are level-sampled; §15 says
  "detects a programmed level", never "on the transition". Firmware consequence, and a trap for any
  future suite: `P1` resets to `0xff`, so **writing a high-detect `KBLS` bit is itself a key out of
  reset** — every setup reads `KBF` once after its register writes to erase what the setup caused.
- `enabled()` **must** be overridden, and `priority_of()` **must** strip `IP_PAIR1`: `ie_mask` does
  double duty (enable-bit selector *and* priority-register mask), the base `enabled()` ANDs that mask
  with the enable cell, and bit 7 of `IEN1`/`IPL1`/`IPH1` is Reserved-but-readable — so without the
  override `MOV IEN1,#0x81` disables the keyboard, and without the strip `MOV IPL1,#0x80` hands it
  level 1. **The strip is a prerequisite for every pair-1 source, so the SPI block inherits it.**
- `clr_bit=false`: the flag belongs to the read of `KBF` (p. 60), not to the interrupt system, so
  `winner->clear()` is a no-op — which is what makes `KBF` = `0x01` observable at the instant the
  vector is taken (`B2`).
- **No `bit_tab51` rows**, and the earlier instruction in this file to add them was wrong: `0x9c`,
  `0x9d`, `0x9e` are not ≡ 0 mod 8, so `KBLS.0` as a bit symbol would be a fiction. The same holds for
  `SPCON` `C3` / `SPSTA` `C4` / `SPDAT` `C5` below.
- Reset ordering: `cl_hw::reset()` runs before `cl_51core::clear_sfr()`, so `reset()` takes `keys`
  from the documented `0000 0000b` constant and never reads the cell back; the `write()` hook (which
  replaces the incoming value with `keys`, making `KBF` genuinely read-only) is what makes the later
  `clear_sfr()` write land consistently.
- Deferred, with a verified cause: a key pressed *while* the core sits in idle, and power-down exit.
  `cl_uc::tick()` ticks hardware only in `stGO`, and `uc51.cc:2045-2047` is
  `if (state == stPD) { //FIXME: tick outsiders eg. watchdog }`. That is a boundary of this tree, not
  a Phase 3 gap — a request that already exists when `idl` is set does end idle (`C10`) — and the same
  boundary is why a watchdog cannot bite in power-down.

### Phase 4 — SPI

`SPCON` `C3` (`SPR2`,`SPEN`,`SSDIS`,`MSTR`,`CPOL`,`CPHA`,`SPR1`,`SPR0`), `SPSTA`
`C4` (`SPIF`,`WCOL`,`SSERR`,`MODF`), `SPDAT` `C5`; 8 master clock rates, full
duplex, programmable polarity/phase, `SS` driven by **general port pins under
software control** (§16.2 — the master uses "four pins of a parallel port"),
`CKCON1.SPIX2` scaling the master clock. Reset is `SPCON` = `0001 0100b`, i.e.
**`MSTR`=1 with `SPEN`=0** — do not reset it to 0. `SPSTA`/`SPDAT` reset values are
contradicted between tables (§3.2). `SSDIS`=1 disables slave-select handling, and the
receiver/error interrupt request is generated **only when `SSDIS`=0**.

**Resolved 2026-11-13 — do not re-open.** Printed p. 9 (`/tmp/ds/p008.txt:11-15`) assigns
`P1.1/T2EX/SS`, `P1.5/CEX2/MISO`, `P1.6/CEX3/SCK`, `P1.7/CEX4/MOSI`, so the four signals do get a
port-shaped exposure rather than staying register-only, and `SS` lands on the very pin `TEXEN`
watches for a timer-2 reload — see the P1 sharing rule at the end of Phase 3, whose timer-2 leg and
four-foreign-bits-readback leg belong to this phase's suite. Proposal stands as written: implement
the shift register, `SPIF`/`WCOL`/`MODF`, clock-rate maths and `SPX2`, expose the four signals on a
`cl_port`-shaped object so a test can loop MOSI→MISO, and defer external-device emulation.

*Test:* master self-loop, one byte per `SPR` combination asserting MC/bit by
differencing; `CPOL/CPHA` sampled-edge check; `SPIF` sets `004Bh` interrupt when
`IEN1.ESPI` set and not otherwise (the D1 lesson, inverted).

**Landed 2026-11-13 — Phase 4 (SPI) cut over.** `rd2_spi.cc` + `rd2_spiccl.h` in
`s51.src/`, registered in `cl_uc89c51rd2::mk_hw_elements()` **only** (never in the shared
`cl_uc89c51r::mk_hw_elements()`, or the AT89C51RB2/RC2 would get a `spi` element and two
interrupt sources aimed at `004Bh` — `t/ident89c51r.py`'s `info hw spi` leg is the guard,
`SIMULATION-PLAN.md` §1.1). `regs51.h` gained the `bmSPR2`/`bmSPEN`/`bmSSDIS`/`bmMSTR`/`bmCPOL`/
`bmCPHA`/`bmSPR1`/`bmSPR0`/`bmSPIF`/`bmWCOL`/`bmSSERR`/`bmMODF`/`bmSPIX2`/`bmESPI` masks; `uc51.cc`
gained nothing (its SFR names and pair-1 arm already existed). `t/spitest.py` — 56 checks, 0 failed,
0.4 s; `t/spi-report.md` is the build report. The P1-sharing timer-2 leg the "Resolved" paragraph
demanded is in `spitest` (a leg runs `TEXEN` alongside the shift register and reads back the four
foreign P1 bits).

Four real model defects were found and fixed before the cutover (`t/spi-report.md` §3): the SS
edge branches were inverted against their own comments (SSERR never fired, CPHA=0 realigned on
release instead of assertion); `SPEN` 0→1 used the cached SCK level so a latch left high by the
program pinned SCK away from `CPOL`; CPHA=1 drove MOSI before the first edge; and `print_info`
printed the event counters under the *same* `WCOL=`/`MODF=`/`SSERR=` keys as the flag line, so a
key=value reader saw a cleared MODF as set — the counters are now `bytes=`/`wcol_events=`/
`modf_events=`/`sserr_events=`. One suite defect is worth remembering: a relative jump whose target
is computed from `len(code)` before the rest of the body is appended aims at itself, so the poll
loop became a spin and every timing leg measured a spin.

### Phase 5 — flash: boot ROM, `ENBOOT`, IAP

Largest item. As of 2026-11-14 items 14 and 15 are in the tree; item 16 is only half there.

14. `AUXR1.ENBOOT` (bit 5) + boot ROM address mapping — **done 2026-11-14**. `cl_rd2_boot` (new
    `rd2_boot.cc` / `rd2_bootcl.h`, `RD2_BOOT_ID 16`) decodes the bit and maps the 2048-byte ROM
    over `F800–FFFF` (table 8-1, printed p.21) with the same `cl_address_decoder` machinery
    `decode_xram()` uses. It is registered in `cl_uc89c51rd2::mk_hw_elements()` and *not* in the
    family class, so the AT89C51RB2/RC2 registers no `boot` element and keeps the flat 64 K mapping
    it always had. `ENBOOT` sits in `AUXR1` one bit-field away from `DPS`; the two are decoded
    independently and `t/dpstest.py` still passes, which is the leg that proves the new decoder did
    not swallow the banker.
15. Intercept `LCALL 0xFFF0` and execute Table 24-10 natively — **done 2026-11-14**. The entry is
    noticed on the instruction boundary in `cl_uc89c51rd2::exec_inst()`, never by hooking the read of
    `FFF0h`: a read hook would also fire on `dump code` and on any `MOVC` of the vector, and a flash
    array that erased blocks in answer to a read would not be a memory. The API's return is issued as
    `instruction_22(0x22)`, which is what makes the "must be done by LCALL instruction to be able to
    comeback" contract (§24.10.1 p.112) hold. Implemented commands: `R1=00h` READ ID (four IDs
    selected by `DPTR0`; an `DPTR0` the table does not list is diagnosed, not answered), `R1=01h`
    ERASE BLOCK (`DPH` must be a block base of table 24-9, else refused with the offending value),
    `R1=02h` PROGRAM DATA BYTE, `R1=09h` PROGRAM DATA PAGE (128-byte page limit and the `A=128 ⇒
    DPL=00h/80h` rule enforced). With `ENBOOT` clear the call is refused by name and *counted*
    (`info hardware boot`), and the array's own byte at `FFF0h` then executes — so firmware that
    forgot the bit cannot pass an API test. Refused with a named reason, because the state does not
    exist yet: `05h`/`06h`/`07h` (SSB/BSB/SBV), `0Ah`/`0Bh` (fuses, HSB), `0Eh`/`0Fh` (boot
    ID/version). There is **no chip-erase command in the IAP API**: Full Chip Erase belongs to the
    bootloader's ISP set (§24.8.2 p.103), so the IAP path erases blocks and pages only.
16. Make the flash array writable through that path only — **half done**. Programming does go
    through the API and nowhere else, and programmed bytes survive `reset` inside a run (asserted).
    **Not** implemented: the host-side backing file, so nothing survives the simulator *process*; and
    the software security levels 0/1/2, which raise monotonically and clear only through that full
    chip erase — until both halves exist the model can neither lock itself nor stay locked. `tWRITE`
    was decided rather than guessed: **0 cycles**, stated at the element so `info hw boot` teaches it.
    The alternative — ~10⁶ machine cycles per byte at 24 MHz (Table 25-7 p.115) — would make every
    flash-touching test measure the write instead of the firmware, which is exactly how a bootloader
    test ends up passing for the wrong reason.

Deliberately **out**: the §24.9 serial bootloader protocol (host-facing, not
on-chip behaviour) and parallel-mode programming — which also means Full Chip Erase
itself stays unimplemented; only its existence and its side effects on the security bytes
(`BSB`/`SBV`/`SSB` back to defaults) are recorded above.

*Test:* `t/bootromtest.py` **68/68** — `LCALL 0xFFF0` with `R1=00h` returns the manufacturer ID in
`ACC` and the `RET` lands back on the caller; erase → program → read round-trips through `MOVC` of
the same array; programmed bytes survive `reset`; the `ENBOOT=0` case refuses by name; and
`-t 89C51R` — the control — has no `boot` element, keeps its flat mapping, and executes its own
array at `FFF0h`, which is what makes the ID and window legs unable to have passed before this.

### Phase 6 — power and the leftovers

17. `PCON.POF` — **done 2026-11-13**: reset `00X1 0000b` means POF (bit 4) is *set* out of reset
    ("set by hardware when VCC rises from 0 to its nominal voltage"), and it is an ordinary
    read/write bit after that ("cleared by software to recognize the next reset type. Can also be
    set by software"), so the model only had to write `bmPOF` at RD2 reset; no hook, no state.
    The power-fail detector thresholds *are* specified (Table 25-7 p.115: `VPFDP` 2.25/2.50/2.69 V,
    `VPFDM` 2.15/2.35/2.62 V, hysteresis 70/140/250 mV, `VCC` slew limit 0.1 V/µs) but which trip
    actually sets `POF` is not, so the comparator stays unmodelled by decision — a flag, not an
    analog circuit. Bit 5 is Reserved-and-indeterminate; the model leaves it 0 and the suite masks it
    out rather than asserting it. No leg asserts the read/write behaviour itself: it cannot fail on
    an SFR cell and would be a vacuous control-passing check (§5).
18. `CKRL` prescaler — **landed 2026-11-13** (prescaler only; per D7 there is no clock-output pin,
    the one documented output being `T2MOD.T2OE` on P1.0). Law from §6.2.1 (printed p.16), where the
    equations extract as split lines: `FCLK_CPU = FCLK_PERIPH = FOSC / (4·(255 − CKRL))` Standard and
    `FOSC / (2·(255 − CKRL))` X2, with **`CKRL = FF` a bypass, never a divide by `255 − 255 = 0`** —
    reset is `FOSC/2` / `FOSC`, and the `CKRL = 00` endpoints `FOSC/1020` / `FOSC/510` fix the line.
    Hence **`M = 1` if `CKRL == 0xFF` else `2·(255 − CKRL)`**.
    **The mechanism as landed is narrower than this item assumed.** No helper is called by BRG, PCA
    and `T2OE`; `ckrl_divider()` (`uc89c51r.cc:170`) multiplies `clock_per_cycle()` (`:203`) once,
    because Fig. 6-1 and Fig. 7-1 both put the prescaler *ahead of* the CPU/peripheral split and
    §6.2.1 writes a single equality — one prescaled net, so the machine cycle and the peripheral
    clock slow together and their ratio never moves. Rule (a) then holds by construction: timers,
    `T2OE`, the PCA timebase, the BRG, the SPI bit clock, the watchdog and every instruction's cycle
    count are machine-cycle derived and divide in time for free, with no code touched and no
    fractional count anywhere. Rule (b) needed exactly one patch, because the only consumers that
    counted raw oscillator clocks were the two fixed UART rates: `serial.cc` accumulates them in
    `FCLK_PERIPH` periods on the RD2 EUART and enters the table rows halved (mode 0 12→6, mode 2
    64/32→32/16), which also flips `fixed_divby()` to *double* for `X2|SIX2`. This item's own trap —
    "if `M` shows up as a second ÷N in `serial.cc`, Phase 2 placed the divisor wrong" — fired in a
    form nobody predicted: the divisor stayed dimensionless, but the *accumulator's unit* had to
    move, because `FXTAL` is upstream of the prescaler and `FCLK_PERIPH` downstream.
    **The SPI needs no patch**, closing the question `t/spi-report.md` §2 left open: §16 states the
    `BD` ladder against `FCLK PERIPH` (footnote p.66 caps slave `SCK` at the bus clock) and
    `rd2_spi.cc` accumulates `fclk_periph_per_cycle()`, which is `CKRL`-invariant per machine cycle
    by construction — `spitest` 56/56 unchanged, `info hw spi` byte-identical. Watchdog: §19 (printed
    p.83) states its periods in **machine cycles** (`(2^14−1) machine cycles`, …) and `CKRL` is named
    nowhere in doc4235 outside printed pp. 4, 8, 15, 16, 18, so the watchdog slowing in *time* is the
    documented behaviour, not an unexamined side effect.
    Gating: `CPU_89C51RD2` (which `globals.cc:384-385` also gives `-t 89C51ED2`) and *not*
    `rd2_mem()`, because this model leaves the RB2/RC2's `0x97` cell unreset and a family-wide reader
    would hand that part `M = 510` out of reset; widening stays a doc4316 question, as for the name
    row. Untouched by decision: the pin-edge counter inputs (no clock-derived net to prescale), the
    keyboard (level sampling, no stated clock), the MDU (`mdu.cc` divides by `clock_per_cycle()` but
    is registered only on the 80517/88x parts, which the gate excludes), the 96-clock reset pulse and
    the 1024-clock power-monitor delay (both counted before the prescaler stage / no pin-level pulse).
    Evidence: `t/ckrltest.py` 35/35, 16 of them failing on the pre-prescaler binary; `ident89c51r`
    36/36 byte-identical, i.e. `CKRL = FF` at reset is a true no-op for every existing transcript,
    which is what clears the UART lattice rewrite. The earlier "boot dump showed `0x97 = 0x00`" was
    the `get sfr` artefact in §5, not a model bug — no `clear_sfr()` change was needed and the
    `rd2registers` reset pin had been truthful all along.
19. `ONCE` (§20, printed p.86) and reduced-EMI (§22, printed p.88) — **closed 2026-11-13 as
    unmodellable, not deferred.** The earlier instruction here ("emit a one-time warning on write")
    was written on an assumption that is false: **ONCE has no register.** §20 invokes it purely from
    pin state — "Pull ALE low while the device is in reset (RST high) and PSEN is high... Hold ALE low
    as RST is deactivated" — after which an external emulator drives the chip, and normal reset
    restores it. There is no SFR to trap, no `AUXR`/`FCON` bit, nothing to warn about; Table 20-1 lists
    only the external pin states. A simulator that does not model ALE/PSEN during reset cannot enter
    or detect it, so the honest state is: out of scope, permanently. Reduced-EMI is `AUXR.AO` and
    belongs to item 20.
20. `AUXR.AO` / `AUXR.M0` / `AUXR.DPU` — **read, then decided not to model**, each with the reason:
    - `AO` (§22, printed p.88): during *internal* code execution ALE simply stops being an **output**
      while staying active internally for `MOVX`, `MOVC` and external fetches, and the pin is weakly
      pulled high. Every observable effect is on a pin; the core's behaviour is byte-identical, so a
      cycle-accurate core model has nothing to reflect.
    - `M0`: selects the `MOVX` RD/WR pulse width (6 vs 30 clock periods). That is bus signalling, not
      instruction timing — `MOVX` remains 2 machine cycles here — so nothing inside the model changes.
      **This is the one bit that becomes real if a pin/bus-level model ever lands**, so the decision is
      recorded rather than buried.
    - `DPU`: disables the port pull-ups, and this model never models pull-ups at all (port reads
      return the latch; port *bit* reads answer 0 — §5). Note the keyboard's own input circuitry shows
      an internal pull-up in its figure (printed p.59) and `rd2_kbd` samples the latch, so `DPU` cannot
      affect the keyboard legs either.
    `AUXR` itself is named (`uc51.cc:190`, `CPU_51R|CPU_ALL_89C51R`) and `XRS`/`EXTRAM` are modelled
    (§2.1), so these three bits are the only unread ones; nothing else in `AUXR` is waiting.

### Phase 7 — **done 2026-11-14** (ED2 data EEPROM)

21. `EECON` `D2` + the 2048 B data EEPROM behind `-t 89C51ED2`, sharing everything from
    Phases 1-6. Landed as `rd2_eeprom.cc` / `rd2_eepromcl.h` (`RD2_EE_ID 17`), registered
    in `cl_uc89c51rd2::mk_hw_elements()` behind `rd2_is_ed2()`.
    - **Gate = the device-row name, not a CPU flag.** `sim51.cc:91` switches on the *exact* value
      of the device-table row, so a combined `CPU_89C51RD2|CPU_89C51ED2` row falls through to
      "Unknown processor type", while a *separate* bit for the ED2 would drop it out of every
      `type & CPU_89C51RD2` test — stripping the XRAM window, X2, CKRL, SPI, keyboard, BRG, boot ROM
      and the SFR names, all of which the ED2 has. Both names therefore keep one flag
      (`globals.cc:385-386`) and the single ED2 difference is taken on `type_str`, which
      `mk_controller` has already normalised, so `strcmp` is exact there. Nothing else in the model
      may key on the name.
    - **Overlay, not a new space or decoder.** The datasheet gives the array `0000h–07FFh` *of the
      XRAM/ERAM space* with `MOVX`, no address/data SFR (printed p.89). `decode_xram()`'s seven
      per-page bankers already own `0000h–06FFh`, decoders in one space are mutually exclusive
      rather than stackable, and the mapping under `EEE` is a function of `AUXR`, so there is nothing
      to bank *to*: the element registers itself as a memory operator on those 2048 cells and decides
      per access. Pinned decisions: `EEE` beats `AUXR.EXTRAM`; `0800h` and above stay on the bus.
    - **`tWRITE` modelled, not zeroed.** Table 25-2 (printed p.115) gives 7 min / 17 max ms; the
      model uses the worst case, in **oscillator clocks** via `clocks_of_time(0.017)` (measured
      203988 clks at `-X 12M` in *both* standard and X2, which is the proof it is time and not
      cycles), so `X2` and `CKRL` cannot shorten it. Byte commits on completion; reads in the window
      answer `0xff`, writes are refused, both counted.
    - **No erase trigger** for this array (contrast Flash §24.2), **no security bit** that a running
      program can set (SSB levels gate the ISP side only), **no backing file** (ucsim has no
      VCC-removal event to hang load/flush on; a default-off named-file option is the only honest
      future form), array deliberately **survives `reset()`**, volatile state does not.
    - `EECON` on the plain RD2 stays named-and-inert exactly as D14 pinned it; `t/xramtest.py`'s
      `eecon_does_not_remap` is the negative control, and `t/eepromtest.py` (30 legs) asserts both
      directions of every leg — array byte *and* the surviving XRAM byte under it.

22. **Open, found by the D15 work:** `cl_timer0::happen` (`timer0.cc:403-404`) samples T0/T1 on a
    **rising** edge while timer 2, the PCA and the interrupt layer use the **falling** edge. Not
    adjudicated: `t/t2cnttest.py`'s T0/T1 legs assert one count per full toggle, so they hold either
    way. Needs the Atmel hardware manual's counter-edge statement before anything changes.

### Phase 8 — board-level mechanics (2026-11-14)

23. Motor + gear + tach wheel as a `cl_hw` plant (`rd2_motor.cc`, `motor[20]`), so the ucsim
    core can be the physics host for a closed control loop instead of only the CPU. Pinned:
    - **Torque domain, not the first-order shortcut.** Drive torque is the linear PM-DC form
      `T_stall * (duty - |w|/w_max)`, so the two knobs are the two the datasheets print
      (stall torque, no-load speed), and it goes negative at `duty = 0`, which *is* dynamic
      braking by a shorted driver. Opposing it: Coulomb friction with a separate static
      (break-away) threshold, viscous drag, and a load torque with a mode bit — `drag`
      (always opposes motion) or `gravity` (fixed direction, so it can run the shaft
      backwards). The gravity mode plus static friction is the whole point: with
      `load_torque < static_friction` the plant **holds** (`held=yes`, zero edges) and above it
      it **runs away**, which is the capability the QEMU `pwm_motor.c` reference states it
      lacks ("no Coulomb friction… never holds against a load torque"). Measured on
      `89C51R` at default cfg: `duty=100 %` → `506.199 rpm`; drive 0 with `load=900 mode=gravity`
      → `-453.470 rpm dir=rev`, edges still flowing; `load=30` → `0.000 rpm held=yes
      pulses=0`; `load=70` → breaks away to `-15.819 rpm`.
    - **Machine cycles are the only time base.** Every constant is cycle-domain, so results
      cannot depend on `-X freq` or on `X2`; `rpm` is printed only because the clock is
      discoverable, and `cycles/rev` is the unit-free ground truth.
    - **The tach output is a full square wave per slot, not a level.** `cl_timer0::happen`
      counts `pins & latch` on a **rising** edge while timer 2 / PCA / the interrupt layer use
      the falling edge, which is item 22 above and still unadjudicated. Emitting both edges
      makes the count identical whichever way that is settled, matching the convention
      `t/t2cnttest.py` already uses (one count per full toggle). Quadrature follows: measured
      gray code `00→10→11→01` forward, `00→01→11→10` reverse, so the direction channel a real
      encoder gives is there for firmware to read on B.
    - **`floor` is reported, never inferred.** Below a speed floor the pins are released and no
      edges come out — a tach wheel's own resolution limit. Firmware measuring "no edges"
      must not conclude "no motion"; the leg asserts `floor=yes`.
    - **Register trap:** P1 comes out of reset at `0xff`, so an unconfigured `pwm_bit` reads as
      **100 % duty** and a leg expecting rest sees ~490 rpm. Clearing the drive bit is part of
      every leg's setup.
    - **Element name is bare in the console**: `info hw motor`, `reset motor`,
      `set hardware motor off`; `info hw motor20` silently falls through to the
      `info hardware category` usage text. Parameters live in `motor_20_cfg` and are read with
      `dump motor_20_cfg 0x00 0x15` (it prints each variable's name) and written with
      `set mem motor_20_cfg <slot> <value>`. `var <name> <value>` must never be used for this:
      it *creates/rebinds* a variable (`help var`) instead of writing the cell.
    - **Gated on `CPU_89C51RD2`, not on `rd2_mem()`** — a real correction, not a tidy-up.
      `rd2_mem()` also covers the RB2/RC2 (its own comment says an RD2-only feature must test
      the flag alone), so the plant first appeared on `89C51R` as well and `t/ident89c51r.py`
      fell from 36/36 to **0/36**. The transcripts contained no behavioural difference at all:
      every diff was the value column sliding right, because the core pads `dump`/`info reg` to
      the **longest registered name anywhere**, and the plant publishes 22 `motor20_*`
      variables (`motor20_static_friction` is 23 characters). Any peripheral with a long
      per-instance name therefore reflows unrelated output on every device that carries it — a
      hazard for any byte-identity suite, and not a defect in the plant. Restricting the gate
      to the two parts doc4235 documents put `89C51R` back to 16 elements, brought
      `ident89c51r` back to 36/36, and left **every** stored baseline untouched: neither
      `89C51RD2` nor `89C51ED2` has a baseline file (`-H` lists 58 device names,
      `test/config/baseline` ships 14). That also removed the need to rebaseline `89C51R` at
      all — and rebaselining it would have been wrong regardless: the file is already stale
      against this fork by ~130 lines (the XRS 1792-byte window, `banks=2` → `banks=16`,
      `eram_chip` → `eram1792_chip`) and its `info reg` line prints *uninitialised* memory, so
      a regenerated copy would bake machine-specific allocator garbage into a counted gate.
      Old-vs-new binaries diff **0 lines** on all 14 config devices apart from the motor row.
    - **Suite: `t/motortest.py`, 48 checks, green**, registered in `t/floor.sh`. Firmware, not
      the console, closes the loop: hand-assembled images park the plant, settle two duty
      windows, wait on an IRAM arm flag, then run a T0-overflow window while **T1 counts the
      wheel as a counter on T1/P3.5**, and the leg compares firmware's own readings against
      `info hw motor`. Headline numbers: `TL1:TH1 = 530` against `pulses = 530`; firmware
      `cycles/rev` within 0.5–0.95 % of the model at every duty; `end=0 ovs=20
      elapsed=1310732` for a 20-overflow window; `held=yes pulses=0` under stiction; `dir=rev`
      with `pulses=330` at duty 0 under gravity; and the gear legs show `out` frozen while the
      slack is open with slack never exceeding its setting. Leg 0 checks the mini-assembler
      against the bed's own disassembler, because the assembler is part of the measurement and
      no C++ gate covers it: three fictions were living in it (`0xC0` is PUSH direct, not
      `SWAP A`; `0x25` is `ADD A,direct`; there is no `JNZ direct` at all), plus a label must
      not consume a byte and `rel = target - (pc + 1)`, not `pc + 2`. It also caught
      `t/kbdtest.py`'s deadline byte `0xd7` (= XCHD) where its own comment said `DJNZ`, so that
      suite's "~510 cycle" deadline never actually waited; it now carries the R7 form
      (`7f ff df fe`) and still reports 30/30.
    - **Open, deliberately not smoothed over — two items.** (a) leg C's polled quadrature
      decoder counts 21 transitions for a wheel that T1 and the model agree is 646; ruled out
      are the plant (C2d agrees to one edge), pin visibility (bit-addressed reads see the level
      low in 39 % of samples), placement (the `LCALL` is the last thing before the deadline
      `djnz`) and the latch-vs-pin effect — so the deficit is unexplained, and `C2c` asserts
      direction purity only. (b) `A-CRBIT` pins **D16** (PCA `CR` registered at `0xda` = CCF2:
      `SETB CR` is inert on the datasheet's own spelling while `CLR CCF2` stops the PCA), so
      the suite starts the PCA with `MOV CCON,#0x40` and that leg is written to fail loudly —
      naming itself for deletion — the moment `pca.cc` is fixed.
---


## 5. Verification rules these phases inherit

Reproducible vectors, hand-checked against the printed error columns — use these
before inventing new ones. `baud = 2^SMOD1 · FPER / (6^(1-SPD) · 32 · (256 - BRL))`
reproduces **both** datasheet tables only with `FPER` = `FCLK_PERIPH` (`Fosc/2`
Standard, `Fosc` in X2); reading `FPER` as `Fosc/12` misses by 6× and 12×.

| config | Fosc | BRL | expected | printed error |
|---|---|---|---|---|
| X2=1 SMOD1=1 SPD=1 (Table 14-5) | 16.384 MHz | 247 | 113 778 (115200) | −1.23 % |
| X2=1 SMOD1=1 SPD=1 | 24 MHz | 243 | 115 385 (115200) | +0.16 % |
| X2=1 SMOD1=1 SPD=1 | 24 MHz | 100 | 9615 (9600) | +0.16 % |
| X2=0 SMOD1=0 SPD=0 (Table 14-6) | 16.384 MHz | 247 | 4740.7 (4800) | −1.23 % |
| X2=0 SMOD1=0 SPD=0 | 24 MHz | 243 | 4807.7 (4800) | +0.16 % |
| X2=0 SMOD1=0 SPD=0 | 24 MHz | 202 | 1157.4 (1200) | −3.55 % |
| X2=0 SMOD1=0 SPD=0 | 24 MHz | 152 | 601.0 (600) | +0.16 % |


- **Never measure a rate from absolute elapsed time.** Difference two runs that
  vary only the reload value; that cancels instruction overhead *and*
  instruction-mix artefacts. `x2test.py` and `hwtest.py` both work this way, and
  it is the method that exposed the `do_t2_reload` bug — a knob looked inert
  because the measurement was insensitive, not because the hardware was.
- **Confirm a defect with an executing simulator, not by re-running a static
  tool against its own output.** D1 was closed only by writing `P1` a different
  marker in each of two ISRs; `PC` alone could not distinguish them, because
  both handlers `RET` back to the same `SJMP $`.
- **Seed distinguishable values** at every address under test, so the observed
  byte names which cell answered.
- Keep the current suites green as the floor (`t/floor.sh [binary]` runs the simulation half), and
  add to them rather than replacing: `langtest` 22/22, `irctest` **50**/50, `kbdtest` **30**/30, `bootromtest` **68**/68,
  `ckrltest` **35**/35, `spitest` **56**/56, `ccapmrst` 12/12, `wdttest` 14/14, `t2oetest` 7/7,
  `x2test` 15/15, `rmwtest` 10/10, `dpstest` 2/2, `hwtest` 8/8, `xramtest` 10/10, `banktest` 3/3,
  `eepromtest` **30**/30 (29 legs when `S51_REF` is unset; the 30th is the RD2 transcript-identity
  leg against the pre-change binary), `t2cnttest` **30**/30 (**9**/30 on any binary without the
  D15 fix),
  `rd2identity` 38/38, `rd2registers` 288/288, `ident89c51r` 36/36 (this is what proves
  `-t 89C51R` is untouched by an RD2-only change — it compares transcripts against
  `/tmp/s51-ref-b`, and since 2026-11-13 one of its legs asks for the `kbd` and `spi` hardware
  categories as well), `euarttest` 34/34, `uartmp` 19/0, `runtests` 11/20 (upstream baseline),
  `xcompare` 61/104 all-four-agree (baseline).
- **The floor can only test the binary it is given, so check that it is.** `t/floor.sh` takes the
  binary as its first argument and exports it as `S51_SIM`; until the SPI cutover eleven of its
  suites hard-coded the vendor path and ignored that variable, so a floor "against the merged
  build" re-tested the vendor tree for most rows while still printing `PASS` for every one of them.
  The class of bug is general: a suite that silently uses a default binary makes every green line
  meaningless, and the tell is the path in the suite's own output (`ident89c51r` prints the binary
  it compared). Any new suite reads `S51_SIM`.
- After any silicon-affecting change, re-run `xcompare.py`: it cross-validates ucsim against a
  **second, independent core** — the Rust `i8051` in `mcs51-rs`, driven by `~/s51/xval` — on
  `(pc, machine_cycles, acc, psw)` for all 104 retired instructions. `pc` and machine cycles agree
  104/104; the `acc`/`psw` gaps are two documented bugs in the *Rust* core, checked against doc4316
  (`README` "Cross-validation"), so 61/104 all-four-agree is the accepted baseline. The
  sim-vs-Ghidra cross-check is a different suite: `rmwtest`, which runs the same bytes through the
  executing simulator and the static language and asserts they agree.
- **Run the two Ghidra suites one at a time.** `langtest` and `rmwtest` drive `analyzeHeadless` with
  fixed project/output names, so two at once produce confident false failures (observed: 21/22 then
  6/22 with nothing wrong but the overlap). A collapse of that size is a collision or a load
  failure, never a modelling disagreement; re-run alone before believing it. `xcompare` is **not**
  one of them — it starts no Ghidra, so it may run alongside either; its own collision hazard is the
  single fixed `/tmp/xval.ihx`, i.e. never two `xcompare` runs at once.
- **A pspec is XML, so a comment must not contain two adjacent hyphens.** One `--` inside a
  `<!-- -->` block makes the language fail to *load*, and the symptom is a suite collapsing to a
  handful of legs, not a parse error. `python3 -c "import xml.dom.minidom as m;
  m.parse('at89c51rd2.pspec')"` before rebuilding costs nothing.
- **A bad Intel-HEX checksum is not rejected.** The loader leaves those bytes zero, zero is
  `NOP`, and the session still reports clock counts — a typo in a hand-written image therefore
  looks like a hardware mystery. `euarttest.py` found it by "measuring" a configuration that had
  never been loaded. Verify the record sums, or `dump code` the image before trusting a number.
- **With `EECON.EEE` set, `set memory xram` is not a backdoor.** On the ED2 the EEPROM element owns
  that window as a memory operator, so a console write into `0000h–07FFh` starts a 17 ms programming
  cycle, and console peeks (`@DPTR`, `state`) consume `blocked_reads`. Either clear `EECON` first or
  read the array through `info hw eeprom` counters. (`t/eepromtest.py` §6 of its report.)
- **`set hardware portN …` can never select a port.** `arg.cc:341-357` prefix-matches the element
  name, so `port1` matches nothing and answers `Error: no hw`. Drive pins through the cfg space
  instead: `set memory port_1_cfg 1 <val>` (`port_pin` is index 1, `port_value` index 2 —
  `portcl.h:40-45`, and index 2 is the mirror).
- **Before changing one line in a shared core file, enumerate the subscribers of whatever it
  notifies.** `port.cc` is one function used by every model in this fork (8051 family *and* STC12),
  so the counter-double-count fix had to be checked against all four `EV_PORT_CHANGED` handlers
  (`timer0.cc:398`, `timer2.cc:399`, `pca.cc:368`, `interrupt.cc:170`) rather than against the
  failing case alone. My floor covers the 8051 family; the STC12 port path is *not* covered by any
  suite here, and that is the honest limit of the verification.
- **`-t` takes `89C51RD2`, not `AT89C51RD2`.** The wrong spelling answers "Unknown processor type.
  Use -H option to see known types.", which reads exactly like a missing device row or a missing
  `S51` export and will send you hunting through `globals.cc` for a row that is already there. `-H`
  lists the accepted names (`89C51R`, `89C51RD2`, `89C51ED2`, …).
- **`get sfr 0x97` does not read the SFR cell.** It printed `0x97[151:1] … 0x0000007f … 127` —
  another space, and a value that is not the register — which is how a nonexistent "CKRL resets to
  0" bug got reported once. Read SFRs with `dump sfr <lo> <hi>` (always an explicit range) and write
  them with `set memory sfr <addr> <val>` (verified on `CKRL`: `0xff` → `0x5a` in the very next
  `dump sfr`). There is no `set sfr`/`set var`, and the *named* form fails — `set memory CKRL 0x97
  0x5a` answers "Error: excess data for bit(s)" because the var resolves as a bitfield — which is how
  "writes must be real `MOV` instructions" got claimed once.
- **A long scripted console run silently drops output rows.** In a `step 1 ; dump ; step 1 ; dump …`
  script the k-th dump is *not* the k-th step — 78 commands produced 65 rows — and the mismatch
  invents timing out of nothing: it turned a 16-machine-cycle `T2OE` period into 13. Any
  step-quantised reading needs its own session, one `step N` plus one query.
- **`get sim_ticks` is not a command.** Oscillator-clock totals come from `state`, whose
  "Total time since last reset= … sec (N clks)" line counts clocks since the last reset.
- **Two bit positions read wrong straight off the datasheet pictures:** `CMOD.CPS` is bits 2:1, so
  `CPS = 01b` is the byte `0x02` and not `0x01`; `CKCON0.T0X2` is bit 1 (`0x02`), `0x04` being
  `T1X2`. Check any mask against the model's own `#define`s before blaming the model.
- **A probe can be the event under test.** For the SPI, *any* read of `SPSTA`/`SPDAT` arms and then
  performs the `SPIF`/`WCOL`/`MODF` clear sequence, so `dump sfr 0xc3 0xc5` erases the flags a leg
  is about to assert, while `info hardware spi` reads the same cells through `get()` and disturbs
  nothing. Every leg therefore takes the instrument reading first and the dump after, inside one
  console session. This is the read-clear trap above, but reached through a command that does not
  look like a read.
- **Compute a relative jump target only after the whole body exists.** Deriving a poll loop's
  `JZ rel` from `len(code)` before the rest of the image is appended aimed it at itself: the loop
  became a spin, every timing leg measured a spin, and each still produced a plausible number.
- **`cl_var_list::add()` deletes any existing binding of the same name** (`del(name)` runs
  first). Two addresses can never share a name, and the second `add` silently unbinds the first
  — hence `RD2_P5_BIT`. Check this before concluding that an SFR name is missing by accident.
- **Under `-e` batch mode `step` does not run reliably**, and a piped console interleaves its own
  echo with results: match rows by address+name adjacency, not by line start. `go` without a
  breakpoint wedges the console.
- **The io console and the UART share `fin`, so anything you pipe in during `go` becomes UART
  bytes, not commands.** A command script that works while stopped goes silent after `go`: the
  remaining lines are fed to the receiver. Drive the console only while halted, or let the program
  stop itself on a breakpoint before you ask it anything.
- **`-S out=<file>` does not capture transmitted bytes in this build** — the file is created and
  stays 0 bytes even on a clean `quit` after a completed transmission, and a `timeout` kill skips
  the flush anyway. Verify TX the way `euarttest.py` does: breakpoint on the `TI` flag and measure
  clocks. Do not conclude TX is broken from an empty capture file.
- **A rate suite run against a binary that lacks the peripheral hangs instead of failing fast**
  (a `JNB TI` loop has no timeout). Give control runs a wall-clock budget so the control is not
  mistaken for a broken suite.
- **`PCON.0` is `bmIDL`, not `SMOD`.** `regs51.h:166-173` has `bmSMOD 0x80`, `bmSMOD0 0x40`,
  `bmPD 0x02`, `bmIDL 0x01`, so a measurement image that writes `MOV PCON,#0x01` meaning
  "SMOD=1" instead parks the CPU in idle: `step` then reports `Stop at 0x000003: (109) stepped 12
  ticks` forever, `go` never reaches its breakpoint, and the console looks wedged — which reads
  exactly like a simulator hang, and was one, until the bytes were dumped. Set `0x80` for SMOD.
  When a leg mysteriously stops retiring instructions, dump the image bytes and check which SFR
  bits the program actually wrote before believing a model defect.
- Suites do not agree on summary wording: `rmwtest`/`xramtest`/`rd2registers` print `PASS:`, while
  `hwtest` prints `8/8 cases match the datasheet` and `euarttest` prints
  `Checks run: 34  Failed: 0`. A `grep '^PASS'` over the whole floor silently reports nothing for
  two of them.
- **A suite must run the device whose feature it measures, and must default to the tree under
  development.** Almost every suite here runs `-t 89C51R`, which was harmless while the model had
  one Atmel identity; now that RD2-only features are gated on `CPU_89C51RD2`, a suite measuring one
  of them under `-t 89C51R` is measuring a device that does not have it. `euarttest.py` did exactly
  that *and* defaulted `SIM` to a `/tmp` build, so it reported 31/31 against a binary that was not
  the one under development. Rules: feature legs use `-t 89C51RD2`; `SIM` defaults to the vendor
  tree with `S51_SIM` as the only override; and a negative control must be a binary that **has**
  the `89C51RD2` device name but lacks the feature (`/tmp/s51-ref-b`) — a pre-identity binary
  passes "no such element / nothing sent" vacuously, because it could not select the device at
  all. The `-t 89C51R` suites survive as the identity guard, which is what `ident89c51r.py`
  exists to check.
- **Any console read of a read-clear (or write-to-clear) register *consumes* the clear.** In
  `kbdtest` the sequence `dump sfr 0x9c 0x9e` followed by `info hardware irq` reported `request=no`
  while a key was held, because the dump read `KBF` and cleared it. That produced a confident wrong
  root-cause hypothesis ("the model's request is sticky") before anyone noticed the probe order.
  Probe with the instrument **first**, dump second; and when a model looks stateful-wrong, check what
  the harness read in between. Applies to every future block with a read-clear or write-1-to-clear
  status register.
- **A check that passes on the feature-less control binary proves nothing, so enumerate them.**
  `kbdtest` leg A1 ("`KBLS`/`KBE`/`KBF` read `0x00` out of reset") passes on `/tmp/s51-ref-b`, which
  has no keyboard at all, because unwritten SFR space reads 0. Run the suite against the control and
  for every check that passes there, either justify it (absence guards *should* pass on both) or
  delete/redirect it to an assertion only the model can satisfy. The control run must still fail fast
  with a named reason and a bounded runtime — 1 PASS / 29 FAIL in ~1 s is the shape to aim at.
- The floor is scripted: **`t/floor.sh [binary]`** runs all 20 sim suites (a `bootromtest`-style file
  joins the run the moment it exists in `t/`) against one binary via `S51_SIM`, prints one summary
  line per suite, and exits with the count of suites that are **not** green, naming them. Its verdict
  rules were rewritten on 2026-11-14 because the old ones lied, three ways: `runtests` is **reported
  but never counted** (it exits non-zero by design at its `11/20` upstream baseline, so
  `floor: 1 suite(s) not green` had never once meant runtests, and every phase note that read the
  count that way — several of them in this file — was reporting a green floor that did not exist);
  the line shown for a suite is now the **last** verdict-pattern match, because a suite that prints
  `ok` rows and then `FAIL: 48/50` used to show its first `ok` and look green; and a single `FAIL`
  line now makes a suite red even when it exits 0. That combination is how `irctest` sat at 48/50
  from the SPI cutover through three more cutovers while the floor reported nothing but the baseline.
  The floor exists because retyping the list is how suites get skipped. Excluded deliberately:
  `langtest`/`rmwtest`, which start a headless Ghidra and must run one at a time, and `xcompare`,
  which starts no Ghidra but writes a fixed `/tmp/xval.ihx`.

- **A suite that has been red for a long time is usually a stale expectation, not a broken model —
  but only after the model has been re-read against the primary source.** `irctest` sat at 48/50 for
  three cutovers. Its two failing legs asserted the *pre-RD2* polling order, in which the keyboard
  interrupt is last, while table 17-2 (printed p.73) gives the SPI priority 10 and the keyboard 8;
  `info hardware irq` on the model prints exactly the table's order. The model was right and the
  suite was wrong, so the legs were replaced and not the model. Re-derive the expectation from the
  datasheet and put the derivation in the leg's name: a leg called "poll order is table 17-2" can be
  audited against the PDF, one called "one source added" cannot. The same sweep is owed after every
  phase lands — statuses, counts and "the one red is X" sentences rot faster than the code does, and
  this file, `README.md` and a phase report were all carrying the same wrong sentence at once.
- **There is no "list the hardware elements" command in this console, so a family-level leak needs a
  per-element absence guard.** `info hardware` and `info hw` with no argument print only
  `info hardware category` (the usage line), so a byte-identity leg built on them compares two
  identical usage strings and can never fail — exactly the vacuous-check class above. `info` has
  exactly six subcommands (`breakpoints registers hardware memory variables history`), and `info
  hardware` takes one category at a time (`irq`, `wdt`, `pca`, …), which `ident89c51r.py` already
  pins for `wdt` and `pca`. Therefore the guard against an `CPU_89C51RD2`-gated element leaking onto
  the AT89C51RB2/RC2 is an explicit per-element assertion in the feature's own suite — `kbdtest` A6
  ("absent on `89C51R`: no element, no source"), which must pass on *both* binaries, and which is the
  reason A6 is the single legitimate control-passing check in that suite. Every new RD2-only block
  owes its own A6.

## 6. Worth upstreaming

D1 (STC block leaking vectors to every 51 derivative), D3, D11 (the shared
`cl_serial` divisor written in machine cycles against a clock accumulator), and the
`do_t2_reload`/`do_t2_capture` fix belong in mainline
`danieldrotos/ucsim` — the `cycles`-ignoring bug is present verbatim in
`master`, `master` still has `uc89c51r.cc`, `clock_per_cycle()` is hardcoded
`12` there, and the tree is actively committed to. See the distribution notes in
`README.md`. Phase 1's device-identity split is a design conversation to have
*before* proposing the feature work.

Two more items came out of the step-10 survey. `cl_uc89c51r::it_priority`
(`uc89c51r.cc:359`, declared `uc89c51rcl.h:50`) is **dead code**: nothing in the tree calls it —
the live arbitration hook is `cl_uc::priority_of()` — so upstream carries a priority function that
has never once run, which is very likely how D13 survived. And D13 itself (`IPH0` inert) is a
classic-part-wide question once doc4234 is in hand, so it is a good second message after the
`do_t2_reload` fix.

A third came out of the name-coverage audit. `cl_uc51r::received()` (`uc51r.cc:126`) implements an
`SM2` OR-match rule and has **zero callers** — the second dead function found in the same family as
`it_priority`, and the reason "what the model already does about `SM2`" reads as existing behaviour
when nothing executes it. Worth a message on its own, because a dead model function is worse than a
missing one: it answers "is this already handled?" wrongly. The audit's fourth candidate is the
caution to quote next to it — `EECON` was missing from `sfr_tab51`, which is *half* right: the
EEPROM behind it is AT89C51ED2-only, but the register is in the family SFR map with a reset value
(D14), so the address was named and the no-op pinned rather than left unknown. Chapter-3 tables in
this family are variant unions; read the map and its reset column, plus the feature list's
qualifiers, before deciding that absence is either a bug or correct.

Two more came out of the receive work, and both are upstream-shaped because neither is RD2
specific. **`cl_serial::conf_op` has no guard against re-entering the notification**: a handler
that calls `cell->set()` on the cell whose write it is handling loops forever, and the symptom is
a simulator pinned at 100 % CPU before the first instruction retires, with no diagnostic. Every
generic slot in that function mirrors the value into a member and never writes the cell, so the
contract is existing practice — but nothing says so, and the trap costs an afternoon the first
time. **`cl_f` drops `0x00`, `ESC` and `0xff` from an input stream** (`-S in=<file>`), which is
invisible unless the dropped byte was meant to be data: a test feeds a file, gets fewer bytes
back than it wrote, and the missing ones are exactly the ones that carry meaning. Both belong in
the same upstream thread as the timer-2 fix, framed as "the byte stream between the console and a
peripheral is lossy and re-entrant in ways that look like model bugs".

The 2026-11-15 review round added three more, and it is worth separating the two that are
general from the one that is latent. **D17 is general**: `cl_serial` reads `AUXR` bit 0 wherever
an `AUXR` cell exists, so any derivative that owns that address for another reason — on the RD2
it is `AO`, the ALE/EMIF control — inherits a baud-clock select it never asked for, and the fix
is a `CPU_STC12` type mask, the idiom `interrupt.cc` already uses. **D19 is latent in shared
code**: `do_t2_clock_out()` publishes `EV_OVERFLOW`, and the serial side counts any
`EV_OVERFLOW` from timer 2 as a bit clock, so the moment a part carries `t2_clock_out` its clock
output starts driving the UART; today no upstream part sets that feature, which is exactly why
nobody has hit it. **D18 is RD2-only for the same reason** — both writes of `T2MODE_CLKOUT` are
gated on the feature (`timer2.cc:130`, `:159`), so the mode cannot leak on a part that lacks it,
but the bookkeeping lives in shared `timer2.cc` and the next part to adopt clock out inherits the
bug. Frame all three as one message with the timer-2 `cycles` fix: the baud clock and the
counter clock are being taken from events that do not say which role the timer is in.
