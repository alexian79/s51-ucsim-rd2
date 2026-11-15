# AT89C51RD2 — per-feature simulator spec (from doc4235K, 05/08)

Source: `/home/oleksiy/s51/doc4235.pdf`, 137 pp. Page numbers = printed page (= PDF page index + 1).
Companion (extension recipe, not repeated here): `/tmp/ucsim-periph-pattern.md`.

Global SFR/reset facts: see §9. `FPER` = `FCLK_PERIPH` = the post-prescaler clock of §6:
Fosc/2 at reset (CKRL=FFh, Standard mode) or Fosc with `CKCON0.X2`=1; `CKRL` is Table 6-1 / §6.2.1,
pp. 15–16 (§7 is the clock tree, Fig. 7-1, p. 18).

## 1. EUART (pp. 48–58)

SFRs (reset values from Table 3-11 p. 8-9 and per-peripheral tables):
| SFR | Addr | Bits 7→0 | Reset |
|---|---|---|---|
| SCON | 98h (bit-addr) | FE/SM0, SM1, SM2, REN, TB8, RB8, TI, RI | 0000 0000b |
| SBUF | 99h | data | XXXX XXXXb |
| SADDR | A9h | addr | 0000 0000b |
| SADEN | B9h | mask | 0000 0000b |
| BRL | 9Ah | BRG reload | 0000 0000b |
| BDRCON | 9Bh | - - - BRR TBCK RBCK SPD SRC | XXX0 0000b |
| PCON | 87h | SMOD1 SMOD0 - POF GF1 GF0 PD IDL | 00X1 0000b |
| T2CON | C8h | TF2 EXF2 RCLK TCLK EXEN2 TR2 C/T2# CP/RL2# | 0000 0000b |

Clock source select, modes 1 & 3 (Table 14-3 p. 52), per-direction mux; `TCLK`/`RCLK` in T2CON,
`TBCK`/`RBCK` in BDRCON. `TBCK`/`RBCK`=1 forces INT_BRG and makes TCLK/RCLK don't-care (X):
| TCLK | RCLK | TBCK | RBCK | Tx clk | Rx clk |
|---|---|---|---|---|---|
| 0 | 0 | 0 | 0 | T1 | T1 |
| 1 | 0 | 0 | 0 | T2 | T1 |
| 0 | 1 | 0 | 0 | T1 | T2 |
| 1 | 1 | 0 | 0 | T2 | T2 |
| X | X | 0 | 1 | T1 | INT_BRG |
| X | X | 1 | 1 | INT_BRG | INT_BRG |
(0/1 and 1/1 rows for TBCK=0,RBCK=1 mirror the Tx side: Tx = T1 if TCLK=0 else T2.)

Baud formulas:
- Mode 0: `Fosc/12`, and in X2 `FCLK_PERIPH/6` = `Fosc/6`. Shift register, no start/stop.
  Source is the `SRC` bit text (Table 14-13 p. 58): "Cleared to select FOSC/12 as the Baud
  Rate Generator (FCLK PERIPH/6 in X2 mode)". `CKCON0.SIX2` (p. 19) then picks 6 or 12 clock
  periods per bit *in X2 only*, so `SIX2=1` puts the bit back at `Fosc/12`.
- Mode 2: `FXTAL/64`, or `FXTAL/32` with `SMOD1`=1 — **stated in this part's own document**, in
  the SCON mode table on printed p. 53 (`/tmp/ds/p052.txt`: the `1 0` row reads "9-bit UART
  FXTAL/64 or FXTAL/32"), the same table that gives mode 0 as "FXTAL/12 (or FXTAL/6 in mode X2)".
  `FXTAL` is the crystal frequency, so the bit lasts **64 / 32 oscillator clock periods** — not
  machine cycles, i.e. 5.33 / 2.67 machine cycles on a 12-clock part, and the fixed rate does not
  track the machine-cycle rate when the clock per cycle changes.
  HISTORY OF THIS LINE, because it has been wrong twice: one revision wrote "`Fosc/64`, or
  `Fosc/32` with `SMOD1`=1" *as if quoted*; the next asserted that doc4235 "states no rate for
  mode 2 anywhere" and gave the bit as "64 machine cycles = 768 oscillator clocks". The first was
  right in substance but had no citation; the second was wrong, and the row was missed because the
  search used `FOSC/` while the table spells the crystal `FXTAL`. The 768 expectation then
  produced a 24× defect claim in SIMULATION-PLAN.md D11. Cite the table row.
- Modes 1 & 3, timer 1 source: `baud = 2^SMOD1/32 × (timer 1 overflow rate)`;
  timer 1 in mode 2 (8-bit auto-reload), TH1=TL1=reload.
- Modes 1 & 3, timer 2 source (TCLK/RCLK=1): overflow rate `/16` (Figure 14-4 p. 51);
  CP/RL2# is ignored and T2 is forced to auto-reload-on-overflow.
- Modes 1 & 3, INT_BRG: see §2.
- `SRC` (BDRCON.0) switches mode 0 from the fixed rate to the internal BRG (Table 14-13 p. 58).

SM2 receive-filter rule (p. 48-49, Table 14-4): SM2 must be cleared in mode 0 (setting it there
has no effect — multiprocessor/address recognition unavailable in mode 0). SM2=1 → RI is set only
if the 9th received bit (RB8) is 1 (mode 1 only "eventually", per p. 49: address recognition may
be enabled in mode 1, where the stop bit takes the place of the 9th data bit and RI additionally
requires a valid stop bit) or the frame matches SADDR/SADEN (modes 2, 3). SM2=0 → RI set on every
valid frame.

Address match (pp. 49–50):
- Given address = SADDR with don't-cares where SADEN bit = 0: `match iff (RX9 & SADEN) == (SADDR & SADEN)`.
  SADEN zeros = don't-care. Individual address needs SADEN = FFh.
- Broadcast address = `SADDR | SADEN`, don't-cares are the zeros of the OR.
- Reset: SADDR=SADEN=00h → given and broadcast = `XX..X` (all don't-care) → replies to any
  address (80C51 compatibility).

FE (framing error, pp. 48, 53): enabled by `PCON.SMOD0`=1, which also makes SCON.7 read as FE
instead of SM0 (SM0 inaccessible while SMOD0=1). Receiver checks each stop bit; invalid stop bit
→ FE=1. FE is cleared only by software or reset — a later valid stop bit does NOT clear it.
When FE feature enabled, RI rises on the stop bit instead of the last data bit.
FE in mode 0: `not specified in datasheet` — framing detection is documented only for the three
asynchronous modes 1/2/3 (p. 48), and mode 0 has no stop bit to check.

TI/RI (Table 14-4 p. 53): TI set by hardware at the end of the 8th bit time in mode 0, or at the
**beginning of the stop bit** in the other modes; RI set by hardware at the end of the 8th bit time
in mode 0 and per Figures 14-2/14-3 in the other modes (with the FE feature enabled, on the stop
bit instead of the last data bit); both cleared only by software. RB8 = 9th bit received (modes 2,
3); in mode 1 with SM2=0 RB8 = received stop bit, and it is unused in mode 0. TB8 = 9th bit sent.
Interrupt: source UART; enable `IEN0.ES` (A8h bit 4); flag = TI or RI (OR'd); vector 0023h.

Gotchas: SCON.7 is a hardware mux on PCON.SMOD0 — a simulator must return FE or SM0 depending on
SMOD0 for both read and write. Table 3-11 shows SCON reset 0000 0000b. Datasheet calls the SPI
status register SPSTR on p. 4 but SPSTA in Table 3-8 (p. 7) — unrelated to UART but a naming
contradiction. Mode 0 with SM2=1 is documented as "no effect".

## 2. Internal Baud Rate Generator (pp. 51–54, 58)

SFRs: `BRL` 9Ah reset 0000 0000b; `BDRCON` 9Bh reset XXX0 0000b (`- - - BRR TBCK RBCK SPD SRC`).
Bits (Table 14-13 p. 58):
- `BRR` (9Bh.4): 1 = start BRG, 0 = stop BRG.
- `TBCK` (9Bh.3): 1 = internal BRG feeds Tx baud clock; 0 = Timer 1/Timer 2.
- `RBCK` (9Bh.2): 1 = internal BRG feeds Rx baud clock; 0 = Timer 1/Timer 2.
- `SPD` (9Bh.1): 0 = SLOW baud rate generator, 1 = FAST (removes a /6 prescale, see formula).
- `SRC` (9Bh.0) "Baud Rate Source select bit in **Mode 0**": 0 = `FOSC/12` bit clock (datasheet
  wording: "FCLK PERIPH/6 in X2 mode"); 1 = the internal BRG clocks the UART in mode 0.
- Bits 7:5 reserved, read indeterminate, do not set.

Structure (Figure 14-5 p. 52-53): `FCLK_PERIPH ÷ 6` → 8-bit down-counter reloaded from `BRL` →
overflow → ÷32 → ×2^SMOD1 → baud clock. `SPD` bypasses the ÷6; `BRR` is the run gate.
Datasheet formula: `Baud_Rate = 2^SMOD1 · FPER / (6^(1-SPD) · 32 · (256 - BRL))` and
`BRL = 256 - 2^SMOD1 · FPER / (6^(1-SPD) · 32 · Baud_Rate)`.

Reload timing: not specified in datasheet beyond the block diagram (8-bit counter reloaded with
BRL, overflows, reloads). Writing BRL while BRR=1: not specified in datasheet.
No interrupt source — the BRG is a clock generator only.

Relation to `CKCON0.SIX2` (p. 19): SIX2 = "Enhanced UART Clock (**Mode 0 and 2**)" — it selects
6 or 12 clock periods per UART peripheral cycle, so it scales only the fixed Mode 0 / Mode 2
rates, not the timer-driven or BRG-driven Mode 1/3 clocks. Valid only when X2=1; no effect in
Standard mode. `CKCON0.X2` moves the BRG's input clock (FCLK_PERIPH) by a factor of 2.

Formula check: the printed formula works only if FPER is the §6 FCLK_PERIPH (Fosc/2 in Standard at
CKRL=FFh, Fosc in X2), which §14 never states. Checks: X2=0/SMOD1=0/SPD=0, Fosc=24 MHz, BRL=202 →
12e6/(6·32·54) = 1157 vs 1200 = 3.55%, exactly Table 14-6; X2=1/SMOD1=1/SPD=1, Fosc=24 MHz,
BRL=100 → 2·24e6/(1·32·156) = 9615 vs 9600 = 0.16%, exactly Table 14-5. Reading FPER as Fosc/12
gives 801 and 1602 — wrong by 6× and 12×.
BRL=00h at reset → 256-count period (slowest); BRL read-back behaviour `not specified in datasheet`.
## 3. PCA (pp. 35–46)

SFRs (reset from Table 3-11 pp. 8–9):
| SFR | Addr | Bits 7→0 | Reset |
|---|---|---|---|
| CCON | D8h bit-addr | CF CR - CCF4 CCF3 CCF2 CCF1 CCF0 | 00X0 0000b |
| CMOD | D9h | CIDL WDTE - - - CPS1 CPS0 ECF | 00XX X000b |
| CCAPM0..4 | DAh DBh DCh DDh DEh | - ECOM CAPP CAPN MAT TOG PWM ECCF | X000 0000b |
| CL / CH | E9h / F9h | 16-bit counter | 0000 0000b |
| CCAP0L..4L | EAh EBh ECh EDh EEh | | Table 3-11 XXXX XXXXb |
| CCAP0H..4H | FAh FBh FCh FDh FEh | | Table 3-11 XXXX XXXXb, Table 13-5 p.41 says 0000 0000b (contradiction) |
Pins: ECI=P1.2, CEX0..3=P1.3..P1.6, CEX4=P1.7.

Counter clock, CMOD.CPS1:CPS0 (Table 13-1 p. 36, its CPS rows spilling to p. 37): `00` = internal
FCLK_PERIPH/6, `01` = internal FCLK_PERIPH/2, `10` = Timer 0 overflow, `11` = external clock on the
ECI/P1.2 pin, **max rate FCLK_PERIPH/4** — edges faster than that cannot be resolved or captured.
`CR` (CCON.6) run control: set/cleared only by software. `CIDL`=1 gates the counter off in idle.
`ECF`=1 → counter overflow sets CF and requests the PCA interrupt.

CCAPMn bits (Table 13-3 p. 40): bit0 `ECCF` enables CCFn to generate an interrupt; bit1 `PWM`
8-bit PWM output on CEXn; bit2 `TOG` match toggles CEXn; bit3 `MAT` match sets CCFn; bit4 `CAPN`
negative-edge capture; bit5 `CAPP` positive-edge capture; bit6 `ECOM` enables the comparator;
bit7 reserved/indeterminate.
Modes (Table 13-4 p. 41, ECOM CAPP CAPN MAT TOG PWM ECCF):
`0000000` No operation · `X10000X` capture +edge · `X01000X` capture −edge · `X11000X` capture
both edges · `100100X` 16-bit software timer/compare · `100110X` 16-bit high-speed output ·
`1000010` 8-bit PWM · `1001X0X` watchdog (module 4 only).

Behaviour:
- Capture (§13.1 p. 43): CEXn sampled for a transition; on a valid edge load `{CH,CL}` into
  `{CCAPnH,CCAPnL}`; interrupt if CCFn **and** ECCFn. CAPP+CAPN both set → either edge.
- Software timer (§13.2 p. 43): ECOM=1 & MAT=1; 16-bit match sets CCFn.
- High-speed output (§13.3 p. 44): TOG+MAT+ECOM=1 → CEXn toggles on every match.
- PWM (§13.4 p. 45): PWM+ECOM=1. 8-bit comparator on CL vs CCAPLn: output **low while CL <
  CCAPLn**, **high when CL ≥ CCAPLn**; on CL overflow FF→00, CCAPLn reloads from CCAPnH. Period =
  256 PCA ticks ⇒ `f_PWM = f_CPS/256`, duty(high) = `(256 − CCAPLn)/256`; all modules share f_CPS.
- `CCFn` set by hardware on match **or** capture, cleared only by software. `CF` set on counter
  overflow, settable by software, cleared only by software.
- **ECOM write side effects are stated for compare + high-speed output ONLY** (§13.2 p. 44, §13.3
  p. 45): before enabling ECOM both CCAPnL/H should hold non-zero, then once ECOM is set a write to
  CCAPnL **clears** ECOM and a write to CCAPnH **sets** it, so software writes L then H. **PWM
  (§13.4 p. 45) never states this** — it refreshes duty via the CCAPLn ← CCAPnH reload on CL
  overflow FF→00 and needs only PWM+ECOM set. ECOM=0 disables the comparator and so stops the
  module driving CEXn; release to normal port I/O is unstated (p. 35 only says port bits "not used
  for the PCA" can serve as standard I/O).
- PCA watchdog (§13.5 p. 46): module 4 only, needs WDTE (CMOD.6)=1; on 16-bit match an **internal
  reset is generated that does not drive the RST pin**. Hold off by changing CCAPn4, changing CL,
  or clearing then re-setting WDTE.
Interrupt: one shared vector 0033h, enable `IEN0.EC` (A8h bit 6), priority PPC (IPL0/IPH0 bit 6).
Sources OR'd: CCF0..CCF4 (each gated by its ECCFn) plus CF (gated by CMOD.ECF).
Gotchas: Table 13-3 labels bit 0 "CCF0" instead of ECCF0; reserved bits CCON.5, CMOD.5:3,
CCAPMn.7 read indeterminate.

## 4. Keyboard interface (pp. 59–62)

| SFR | Addr | Bits | Reset |
|---|---|---|---|
| KBLS | 9Ch | KBLS7..0 | 0000 0000b |
| KBE | 9Dh | KBE7..0 | 0000 0000b |
| KBF | 9Eh | KBF7..0, read-only | 0000 0000b |
Interrupt: 8 sources, one vector 003Bh, global enable `IEN1.KBD` (B1h bit 0), priority
`IPL1.KBDL` (B2h.0) / `IPH1.KBDH` (B3h.0).
- Inputs are P1.0–P1.7 alternate function, internal pull-up, 8×n matrix.
- **Level detect**, not edge: `KBLS.x`=0 detects a low level on P1.x, 1 detects a high level.
- `KBF.x` set by hardware whenever P1.x shows the programmed level (independent of KBE).
- Interrupt request = `KBF.x AND KBE.x` (Figure 15-2); KBE.x=0 masks the request and leaves the
  pin usable as ordinary I/O.
- KBF is read-only and **all flags are cleared by reading the register** (Table 15-1 p. 60);
  the per-bit text also says "must be cleared by software" — the read is the clear. A simulator
  must clear the whole byte on any read.
- Keyboard inputs exit Idle and Power-down (§15.0.2 p. 59 → §12 p. 80).
Gotchas: Table 15-1 cites a nonexistent "KBIE register" ("KBKBIE.7 bit in KBIE register") — the
register is KBE. P1 output-latch interaction with keyboard detection: `not specified in datasheet`
beyond the internal-pull-up schematic.

## 5. SPI (pp. 63–71)

| SFR | Addr | Bits 7→0 | Reset |
|---|---|---|---|
| SPCON | C3h | SPR2 SPEN SSDIS MSTR CPOL CPHA SPR1 SPR0 | 0001 0100b |
| SPSTA | C4h | SPIF WCOL SSERR MODF - - - - | Table 3-11 0000 0000b; Table 16-4 p.71 00X0 XXXXb |
| SPDAT | C5h | R7..R0 | Table 3-11 XXXX XXXXb; Table 16-5 "Indeterminate" |
| CKCON1 | AFh | bit0 SPIX2, 7:1 reserved | XXXX XXX0b |
Interrupt: vector 004Bh, enable `IEN1.ESPI` (B1h bit 2), priority `IPL1.SPIL` (B2h.2)/`IPH1.SPIH`
(B3h.2). Two request sources (Table 16-2 p. 68): SPIF → "SPI Transmitter" request; MODF →
"SPI Receiver/error" request **only if SSDIS=0**.

Master clock (§16.2.5 p. 64, Table 16-1): `f_SCK = FCLK_PERIPH / BD`, BD = 2^(SPR + 1)
→ 2, 4, 8, 16, 32, 64, 128 for SPR2:SPR0 = 000..110; **SPR=111 is "Don't Use / No BRG"
(Table 16-1) and "Invalid" (Table 16-3)** — no defined behaviour.
CPHA/CPOL (Table 16-3 p. 69, Figures 16-4/16-5 p. 67): CPOL = idle SCK level (0 → idle low,
1 → idle high; "no significant effect on the transmission format"). CPHA=0 → data sampled when
SCK **leaves** the idle state; CPHA=1 → sampled when SCK **returns** to idle. Equivalently
CPOL=0/CPHA=0 sample on rising, shift on falling; CPOL=0/CPHA=1 shift on rising, sample on
falling; CPOL=1 mirrors both.
- CPHA=0: the first SCK edge is the MSB capture strobe, so the slave must drive MISO before
  edge 1 and a **falling edge on SS starts the transmission**; SS must be toggled high then low
  between bytes (Figure 16-6).
- CPHA=1: master drives MOSI on the first SCK edge, the slave uses that edge as start; SS may
  stay low between bytes.
- MSB first, 8 bits per byte, full duplex.
Master (MSTR=1): write SPDAT → byte goes to the shift register if empty; SCK runs 8 cycles; on
completion SPIF=1 **and** the received byte is copied into the receive register.
Slave (MSTR=0): SS must be 0 for the whole message; after a byte enters the shift register it is
immediately copied to the receive register and SPIF is set; software must read SPDAT before the
next byte arrives. Slave must have written SPDAT at least one bus cycle before the master starts,
else the previous shift-register content is re-sent. Max SCK in slave mode = `FCLK_PERIPH/4`
(p. 66 body) — footnote 3 on the same page says "the bus clock speed" (**contradiction**).
Flags:
- `SPIF` set by hardware on transfer completion; cleared by software reading SPSTA with SPIF set
  **then** reading SPDAT.
- `WCOL` set by a write to SPDAT during a transmit sequence; no interrupt; transfer continues;
  cleared by an access to SPSTA followed by an access to SPDAT.
- `SSERR` set when SS goes high before the end of a received byte (slave mode); no interrupt;
  cleared only by clearing SPEN (reset of the SPI state machine).
- `MODF` set when the SS level is inconsistent with the configured mode (SSDIS=0 and SS→0);
  effect = interrupt request + SPEN cleared (SPI disabled) + MSTR cleared. Cleared by reading
  SPSTA with MODF set then writing SPCON; SPEN may then be restored to its prior state.
- Overrun (master sends while slave SPIF uncleared): a read of SPDAT returns the byte sent after
  the last SPIF clear, all others lost — **not detected by the SPI peripheral** (no flag).
SSDIS (pp. 64, 69): set = SS disabled as a select in master and slave modes, freed as
general-purpose I/O, and no MODF request; in slave mode it has no effect when CPHA=0. Footnotes
p. 64: (1) "Clearing SSDIS control bit does not clear MODF"; (2) "Special care should be taken not
to set SSDIS control bit when CPHA = '0' because in this mode, the SS is used to start the transmission." A master drives its slaves' SS from ordinary parallel-port pins (Figure 16-1, §16.2).
`SPEN`=0 disables the SPI immediately (also the SSERR/state-machine reset). Registers may be
accessed any time when no exchange is in progress; during a transfer do not change SPR2:SPR0,
CPHA/CPOL or MSTR, and writing SPDAT causes an overflow. Footnotes p. 66: configure as Master or
Slave **before** setting SPEN, the master before the slave; change CPOL/CPHA only with SPEN=0.
`CKCON1.SPIX2` (pp. 19–20): valid only when `CKCON0.X2`=1; cleared → 6 clock periods per SPI
peripheral cycle, set → 12. §7 thus implies the SPI divider runs off Fosc/6 (X2=1, SPIX2=0) or
Fosc/12, which does **not** match §16's "FCLK PERIPH / 2 …" (FCLK_PERIPH = Fosc/2 or Fosc per §6) —
the two sections are unreconciled; implement §16's table.

## 6. Interrupt system (pp. 72–78)

SFRs: `IEN0` A8h (bit-addr) EA EC ET2 ES ET1 EX1 ET0 EX0, reset 0000 0000b; `IEN1` B1h (bit-addr)
`- - - - - ESPI - KBD`, reset XXXX X000b; `IPL0` B8h (bit-addr) `- PPCL PT2L PSL PT1L PX1L PT0L
PX0L`, reset X000 0000b (Table 3-3 p. 5 writes the UART bit as `PLS`, Table 17-4 p. 75 as `PSL`);
`IPH0` B7h `- PPCH PT2H PSH PT1H PX1H PX0H PX0H`, reset X000 0000b; `IPL1` B2h (bit-addr)
`- - - - - SPIL TWIL KBDL`, reset XXXX X000b; `IPH1` B3h `- - - - - SPIH - KBDH`, reset
XXXX X000b (Table 3-3 p. 5 omits the gap and shows `SPIH KBDH` at 2:0; Table 17-6/17-7 put bit 1
at Reserved — IEN1's own table (p. 77) also lists bit 1 as Reserved).
Four levels from the {H,L} bit pair (Table 17-1 p. 73): `00`=0 lowest, `01`=1, `10`=2, `11`=3
highest — i.e. L is the +1 bit, H the +2 bit. Level is per source, encoded by that source's own
IPLx/IPHx pair.
Sources/vectors (Table 17-2 p. 73), with in-level polling rank:
| # | Poll | Source | Request | Vector |
|---|---|---|---|---|
| 0 | 0 | Reset | – | 0000h |
| 1 | 1 | INT0 | IE0 | 0003h |
| 2 | 2 | Timer 0 | TF0 | 000Bh |
| 3 | 3 | INT1 | IE1 | 0013h |
| 4 | 4 | Timer 1 | **IF1** (sic, TF1) | 001Bh |
| 5 | 6 | UART | RI+TI | 0023h |
| 6 | 7 | Timer 2 | TF2+EXF2 | 002Bh |
| 7 | 5 | PCA | CF + CCFn | 0033h |
| 8 | 8 | Keyboard | KBDIT | 003Bh |
| 9 | 9 | – | – | 0043h |
| 10 | 10 | SPI | SPIIT | 004Bh |
Rules (§17.1 p. 73): a low-priority interrupt can be interrupted by a higher-priority interrupt
but not by another low-priority one; a highest-priority interrupt cannot be interrupted by any
source. Simultaneous different-level requests → the higher level is serviced; simultaneous
same-level requests → resolved by the internal polling sequence above, so the polling rank is the
second-level priority *within* each level (note PCA ranks above UART and both Timers here).
Lower levels never preempt a higher one; same level never preempts.
`EA` (IEN0.7) is the global enable — clearing it disables all interrupts at once.
0043h is a **reserved vector with no source and no enable bit**. The only trace of a TWI
peripheral is the name `TWIL` printed in the IPL1 bit-layout row (p. 78) while the same table's
bit-1 description says Reserved; there is no TWI peripheral, enable or flag anywhere in the
document — treat bit IPL1.1 as an ordinary reserved/indeterminate bit.
Gotchas: §17 says "9 interrupt vectors" but Table 17-2 lists 10 (Reset included) plus the empty
0043h row; IEN0 bit 6 is `EC` (PCA enable) yet Figure 13-2 p. 39 refers to it as "IEN0.6" and
labels EC at IEN0.7/EA at IEN0.6 in that figure's own diagram — trust Table 3-3 (EA=7, EC=6).
Reserved bits read indeterminate; IEN1/IPH1/IPL1 reset with X in bits 7:3.

## 7. Power management + oscillator prescaler (pp. 15–16, 20, 80–83)

`PCON` 87h reset 00X1 0000b: SMOD1 SMOD0 - POF GF1 GF0 PD IDL. Table 18-2 p. 83 restates PCON
with bits 7:4 Reserved (`XXXX 0000b`), dropping SMOD1/SMOD0/POF — contradiction; Table 14-12 p. 57
and Table 21-1 p. 87 both use `00X1 0000b`.
- **Idle**: set `IDL`; the instruction that sets IDL is the last one executed. CPU clock frozen,
  **peripherals keep running**, PC/PSW/SFR/RAM retained. Exit (1) any enabled interrupt —
  hardware clears IDL, execution resumes at the ISR then at the instruction after the IDL setter;
  GF0/GF1 may record that an interrupt occurred during Idle; (2) reset — clears IDL
  asynchronously, execution momentarily resumes after the IDL setter (may run several cycles,
  internal RAM not accessible, so that next instruction must not write a port pin or external
  RAM) then the internal reset algorithm vectors to C:0000h. `CMOD.CIDL`=1 additionally gates the
  PCA counter off during Idle. Port state during Idle: all ports hold their data; ALE/PSEN high
  (Table 18-1 p. 82).
- **Power-down**: set `PD`; stops the oscillator, freezes all clocks at known states; PC, PSW,
  SFR, RAM preserved; VCC may be dropped to VRET once PD is entered. Three exits: (1) an enabled
  **external** interrupt INT0#/INT1# — must be configured level-sensitive and given the highest
  priority, and held long enough for the oscillator to stabilise; execution resumes only when the
  input is released, first the ISR then the instruction after the PD setter; SFRs and IRAM
  unaffected; (2) reset — clears PD asynchronously and directly, redefines all SFRs but leaves
  internal RAM; (3) an enabled external **keyboard** interrupt, behaving like (1).
  Idle + PD set together → Power-down wins, and Idle is *not* entered on the way out of
  Power-down.
- `PCON.SMOD1` (bit 7) = UART double baud rate in modes 1, 2, 3. `PCON.SMOD0` (bit 6) is **not**
  a baud bit: 0 selects SCON.SM0, 1 selects SCON.FE (framing-error mode). They are unrelated.
- `CKRL` 97h (Clock Reload Register, reset **1111 1111b**, bits 7:0 = prescaler value): an 8-bit
  divider between oscillator and CPU+peripherals. CKRL=FFh (the reset state) is a bypass:
  `FCLK_CPU = FCLK_PERIPH = Fosc/2` in Standard, `Fosc` in X2. For CKRL ≠ FFh:
  `FCLK = Fosc / (2 · (255 − CKRL))` in X2 mode, halved once more in Standard, so CKRL=00h gives
  Fosc/510 (X2) / Fosc/1020 (Standard); all peripheral dividers are relative to FCLK_PERIPH.
  CKRL itself has no clock-output function (`not specified in datasheet`); the only documented
  clock output is Timer 2's (§12.2 p. 31): set `T2OE` (T2MOD C9h) with C/T2#=0, TL2 clocked at
  FCLK_PERIPH/2, RCAP2H/L reloaded on overflow with no interrupt, 50 % duty, output on the T2 pin
  (P1.0), range FCLK_PERIPH/2^16 … FCLK_PERIPH/4.
- X2 mode (§7 pp. 17–20): `CKCON0.X2` = 12 vs 6 clock periods per machine cycle; it is loaded at
  power-up from the Hardware Security Byte, default cleared, so CKCON0 reset = 0000 000'HSB.X2'b
  (Table 7-1) whereas Table 3-11 p. 9 shows 0000 0000. T0X2/T1X2/T2X2/SIX2/PCAX2/WDX2 (CKCON0)
  and SPIX2 (CKCON1) are **inert unless X2=1**; cleared = 6 clock periods per peripheral cycle
  (fast), set = 12 (slow) — all six share that polarity. Table 3-2 p. 4 names bit 6 `WDTX2`,
  Table 7-1 p. 19 names it `WDX2`; the prose on p. 18 calls them WdX2/UartX2/PcaX2.
Gotcha: idle/power-down exit leaves the *next* instruction executable while IRAM is inaccessible.

## 8. Hardware watchdog timer (pp. 84–85)

`WDTRST` A6h reset XXXX XXXXb, **write-only** (a read returns indeterminate data); `WDTPRG` A7h
reset XXXX X000b, bits 2:0 = S2:S1:S0, bits 7:3 reserved/undetermined.
- 14-bit counter + a 2^7-range extension selected by WDTPRG. Counter increments **every machine
  cycle** while the oscillator runs.
- Disabled on exiting reset. Enable = write **01Eh then 0E1h** in sequence to WDTRST (0A6h)
  (Table 19-1 p. 83; `01h` is not the first byte — writing `01h,0E1h` leaves the WDT off).
  Refresh = the same 01Eh/0E1h sequence. **No way to disable it except reset** (hardware reset or
  WDT overflow reset). The counter cannot be read or written.
- Period: `t_WDT = (2^(14+S) − 1) machine cycles`, S = S2:S1:S0 (0..7), i.e.
  (2^14−1)…(2^21−1) machine cycles = 16.3 ms, 32.7 ms, 65.5 ms, 131 ms, 262 ms, 542 ms,
  1.05 s, 2.09 s at FoscA = 12 MHz (Table 19-2 p. 85; the last two rows are misprinted "1.05 ms"
  and "2.09 ms" while the prose says "from 16 ms to 2s"). One machine cycle = 12 (or 6) oscillator
  periods, and `WDX2`/`X2` change it — the CKCON0 bit is **`WDX2`** in Table 7-1 (p. 18) but is
  spelled `WDTX2` in the front-matter list on p. 4, so both spellings in circulation are the same
  bit — and the period scales with X2 and with CKRL.
- WDTPRG must be configured **before** the activation sequence and cannot be modified until the
  next reset.
- **No window**: there is no early-refresh prohibition, no two-sided window, no warning interrupt
  — refreshing at any time before overflow is legal (`not specified in datasheet`: any window
  feature). Overflow action = **device reset**, and it drives a **HIGH pulse on the RST pin** of
  `96 × TCLK_PERIPH` duration (Figure 10-1 p. 26 lists the WDT as a Reset-output source; a 1 kΩ
  series resistor is recommended to propagate it). Contrast §13.5: the PCA watchdog's internal
  reset does *not* drive RST.
- In Power-down the oscillator stops ⇒ WDT stops; the WDT is not started until the exiting
  interrupt is released; reset the WDT just before entering Power-down. In Idle the oscillator
  runs ⇒ the WDT keeps counting and must be serviced by a timer that periodically exits Idle.
Interrupt: none — the WDT has no interrupt path, only reset.
## 9. Reset (§10, pp. 26–27)

Four reset sources (Figure 10-1): Power Monitor (internal POR/PFD), Hardware Watchdog, PCA
Watchdog, and the RST input pin. Internal reset is generated by POR/PFD or the hardware WDT.
- RST pin has an internal **pull-down**, so a capacitor to VCC alone gives a power-on reset; an
  external circuit can force a pulse longer than the Power-Monitor-controlled internal reset. A
  high on RST for **two machine cycles** while the oscillator runs resets the device (Table 4-1
  p. 21), and the pin becomes an **output** when the hardware watchdog forces a reset.
- Power Monitor (§11 p. 28): once XTAL1 swings above/below VIH/VIL, a **1024 clock period delay
  extends** the reset coming from the Power Fail Detect; if the regulator's supply falls below the
  PFD threshold the reset is applied **immediately**. Threshold values: `not specified in
  datasheet` (electrical §25 gives VIH/VIL only, no PFD/POR voltage).
- WDT reset drives RST HIGH for 96 × TCLK_PERIPH (needs a 1 kΩ series resistor to propagate into
  external circuitry); the PCA watchdog's internal reset does not reach the pin.
- On reset the CPU vectors to C:0000h. After a Power-down-exit reset, execution may run a few
  cycles before the internal reset algorithm takes control (IRAM not accessible in that window).
SFR reset values that differ from a plain 8052 (Table 3-11 pp. 8–9):
`P4 C0h=FFh`, `P5 E8h/C7h=FFh`, `CKRL 97h=FFh`, `AUXR 8Eh=0X00 1000b` (EXTRAM=1, AO=0, XRS=000,
DPU=0), `AUXR1 A2h=0XXX X0X0b` (bit 5 stuck 0 in the table), `CKCON1 AFh=XXXX XXX0b`,
`BRL 9Ah=00h`, `BDRCON 9Bh=XXX0 0000b`, `SADDR A9h=00h`, `SADEN B9h=00h`,
`KBLS 9Ch/KBE 9Dh/KBF 9Eh=00h`, `SPCON C3h=0001 0100b` (table 16-3 order ⇒ **MSTR=1, CPHA=1,
SSDIS=0, SPEN=0** at reset — the earlier gloss here said SSDIS=1, which the bit order refutes),
`SPSTA C4h=0000 0000b`, `SPDAT C5h=XXh`, `CCON D8h=00X0 0000b`, `CMOD D9h=00XX X000b`,
`CCAPMn DAh-DEh=X000 0000b`, `CL E9h/CH F9h=0000h`, `EECON D2h=xxxx xx00b` (ED2),
`T2MOD C9h=XXXX XX00b`, `WDTPRG A7h=XXXX X000b`, `WDTRST A6h=XXXX XXXXb`,
`IEN1 B1h/IPL1 B2h/IPH1 B3h=XXXX X000b`, `IPH0 B7h=X000 0000b`, `IPL0 B8h=X000 0000b`
(Table 3-11 prints IPL0 as "X000 000" — 7 digits, typo), `PCON 87h=00X1 0000b` with POF set only
after a cold reset (a warm reset leaves POF), `SP 81h=07h`, `CKCON0 8Fh=0000 0000b` per Table 3-11
but 0000 000'HSB.X2'b per Table 7-1 p. 19 (HSB-driven).
POF is the only reset-conditional bit: set when VCC rises from 0 to nominal, unaffected by warm
reset. Nothing in §10 says which SFRs are *not* reset; only Table 3-11 enumerates the values.

## 10. Power-off flag (§21 p. 87) and Power Monitor (§11 p. 28)

- `PCON.POF` (87h bit 4), reset 1 after power-on. Set by hardware when VCC rises from 0 to its
  nominal voltage (cold start); can also be **set or cleared by software**; cleared by software to
  recognise the next reset type. A warm start (VCC never removed, e.g. exit from Power-down) does
  not set it (Table 14-12 p. 57: "A warm reset doesn't affect the value of this bit").
- Power Monitor = regulator + POR + Power Fail Detect on the *internal* regulated supply of the
  core, memories and peripherals; undervoltage applies an **internal reset** to them (external
  VCC spikes are smoothed by the regulator). Start-up: reset extended 1024 clock periods after
  the oscillator starts (§9). Thresholds (§11 p. 29 names VPFDP/VPFDM; §25 p. 115 values min/
  typ/max): VPFDP 2.25/2.50/2.69 V, VPFDM 2.15/2.35/2.62 V, hysteresis 70/140/250 mV.
Gotcha: because POF is software-writable, a simulator must treat bit 4 of any PCON write as a
plain writable bit and only force it on the cold-start event.

## 11. Flash / EEPROM memory and IAP API (§24, pp. 93–114)

Geometry: 64 KB program Flash = **512 pages of 128 bytes**; byte-wise read, no wait state.
Programming is by byte or 128-byte page and **includes a self-erase**, so no separate erase is
needed first. Byte/page erase+program = **10 ms** (§24.1) and tWRITE = 7–17 ms (§25); typical 64 KB
= 22 s via the bootloader; 100K write cycles; 10-year retention. Erase granularity is the **block**:
block0 0000h-1FFFh, block1 2000h-3FFFh, block2 4000h-7FFFh, block3 8000h-BFFFh, block4 C000h-FFFFh
(p. 111); `ERASE BLOCK` picks it via DPH and returns `ACC = DPH`, and `PROGRAM DATA PAGE` must stay
in one page (A=128 ⇒ DPL = 00h or 80h). EA high = internal fetch, EA low = all fetches external.
Security — Hardware Security Byte (HSB, Table 24-1 p. 94): bit 7 X2 (programmed=0 → force X2 mode
after reset, default unprogrammed = X1), bit 6 BLJB (programmed=0 → boot loader starts at **F800h**,
unprogrammed=1 → user app at 0000h, **default programmed** so ISP is enabled), bits 5:4 reserved,
bit 3 XRAM (programmed inhibits XRAM), bits 2:0 LB2:LB0 lock bits (parallel tools only).
Program lock bits (Table 24-2 p. 95; P=0, U=1, X=don't care) protect code through the *parallel*
interface, default level 4:
| Level | LB0 | LB1 | LB2 | Effect |
|---|---|---|---|---|
| 1 | U | U | U | no lock features |
| 2 | P | U | U | MOVC from external program memory cannot fetch internal code; EA sampled and latched at reset; further parallel programming disabled; ISP and API programming still allowed |
| 3 | X | P | U | as 2 + verify through the parallel interface disabled |
| 4 | X | X | P | as 3 + external execution disabled (**default**) |
Software Security Byte (SSB, in the XAF "eXtra Array Flash") gates the **ISP** interface and can
only be programmed to a *higher* level (§24.8.1 p. 103): 0 NO_SECURITY=FFh (default), 1
WRITE_SECURITY=FEh (no write of Flash/BSB/SBV, bootloader answers 'P'), 2 RD_WR_SECURITY=FCh (no
read or write, answers 'L'). Table 24-7 p. 103: Erase Block is disallowed from level 1 up, Full
Chip Erase and Blank Check always allowed; only a full chip erase resets the bits. Parallel
programming of a new application likewise needs a chip erase first (HSB returns to inactive).
Boot mapping: `AUXR1.ENBOOT` (A2h bit 5)=1 → boot ROM mapped to **F800h–FFFFh**; BLJB fuse selects
F800h (programmed, default) vs 0000h; `SBV` holds the high address of a customer bootloader,
default **FCh**, called as `JMP [SBV]00h` (FC00h); `BSB` = Boot Status Byte; API entry `USER_CALL`
at **FFF0h**.
API rules (§24.10 p. 112): set R1 (command), ACC, DPTR0, DPTR1 then **LCALL 0FFF0h** (must be LCALL
so the API can return); `AUXR1.ENBOOT` must already be set; the bootloader **does not disable
interrupts** — the user must disable them before the call (and around EEPROM-data access) and
re-enable after; the hardware WDT must be handled by the user before any Flash operation.
Table 24-10 API Call Summary (pp. 112–113) — XX = don't care:
| Command | R1 | A | DPTR0 | DPTR1 | Returned | Effect |
|---|---|---|---|---|---|---|
| READ MANUF ID | 00h | XXh | 0000h | XXh | ACC = Manufacturer Id | read manufacturer id |
| READ DEVICE ID1 | 00h | XXh | 0001h | XXh | ACC = Device Id 1 | read device id 1 |
| READ DEVICE ID2 | 00h | XXh | 0002h | XXh | ACC = Device Id 2 | read device id 2 |
| READ DEVICE ID3 | 00h | XXh | 0003h | XXh | ACC = Device Id 3 | read device id 3 |
| ERASE BLOCK | 01h | XXh | DPH=00h/20h/40h/80h/C0h → blocks 0-4 | 00h | ACC = DPH | erase block 0-4 |
| PROGRAM DATA BYTE | 02h | value to write | byte address | XXh | ACC = 0: DONE | program up to one Flash data byte |
| PROGRAM SSB | 05h | XXh | DPL=00h→level 1, 01h→level 2, 10h→level 0, 11h→level 1 (DPH=00h) | 00h | ACC = SSB value | set Software Security Byte level |
| PROGRAM BSB | 06h | new BSB value | 0000h | XXh | none | program Boot Status Byte |
| PROGRAM SBV | 06h | new SBV value | 0001h | XXh | none | program Software Boot Vector |
| READ SSB | 07h | XXh | 0000h | XXh | ACC = SSB | read Software Security Byte |
| READ BSB | 07h | XXh | 0001h | XXh | ACC = BSB | read Boot Status Byte |
| READ SBV | 07h | XXh | 0002h | XXh | ACC = SBV | read Software Boot Vector |
| PROGRAM DATA PAGE | 09h | number of bytes | first Flash address | XRAM address of first data byte | ACC = 0: DONE | program up to 128 bytes, must stay inside one page (A=128 ⇒ DPL = 00h or 80h) |
| PROGRAM X2 FUSE | 0Ah | fuse value | 0008h (00h or 01h) | XXh | none | program the X2 fuse bit with ACC |
| PROGRAM BLJB FUSE | 0Ah | fuse value | 0004h (00h or 01h) | XXh | none | program the BLJB fuse bit with ACC |
| READ HSB | 0Bh | XXh | XXXXh | XXh | ACC = HSB | read Hardware Security Byte |
| READ BOOT ID1 | 0Eh | XXh | DPL=00h | XXh | ACC = ID1 | read boot id 1 |
| READ BOOT ID2 | 0Eh | XXh | DPL=01h | XXh | ACC = ID2 | read boot id 2 |
| READ BOOT VERSION | 0Fh | XXh | XXXXh | XXh | ACC = Boot_Version | read bootloader version |
Gotchas: Table 24-10 lists no chip erase, but the **bootloader ISP command set (§24.9 p. 111) has
R1=07h Full Chip Erase (~6 s): fills user Flash with FFh, restores BSB=FFh/SBV=FCh/SSB=FFh, leaves
the bootloader intact (p. 103-104), and stays allowed at SSB level 2**. Also PROGRAM SSB lists
DPL=00h and 11h both as "level 1" (misprint); both fuse commands use R1=0Ah, split only by DPTR0.

## 12. Port 4 and Port 5 (pp. 11, 19, 21)

`P4` C0h 8-bit; `P5` **E8h bit-addressable** and **C7h byte-addressable**; internal pull-ups, reset
1111 1111b (Tables 3-4 p. 5, 3-11 pp. 8–9). Same quasi-bidirectional I/O as ports 1-3 (write 1 →
pulled high, usable as input; a pulled-low input sources pull-up current); **no drive or latch-mode
control bits exist for P4/P5** (`not specified in datasheet`); `AUXR.DPU` disables pull-ups
device-wide; P5 is also listed as `PI2` on p. 4. Pins (Table 4-1 pp. 21-22; PLCC44 | VQFP44 |
PLCC68 | VQFP64): P4.0-4.7 = `—, —, 20 24 26 44 46 50 53 57, 11 15 17 33 35 39 42 46`, P5.0-5.7 =
`—, —, 60 62 63 7 8 10 13 16, 49 51 52 62 63 1 4 7` ⇒ **both ports only on PLCC68/VQFP64**.
Gotchas: the p. 21 write-ups of both ports are copy-paste errors describing "Port 3 pins"; the §25
DC tables cover "ports 1, 2, 3, 4" only — no Port 5 spec.

## 13. ONCE mode (§20 p. 86)

On-Chip Emulation, entered by pins and not by an SFR: pull ALE low while RST is high and PSEN is
high, and hold ALE low as RST is released; an emulator or test CPU then drives the circuit. Pin
status (Table 20-1): ALE, PSEN, Ports 1/2/3 and Port I2 weak pull-up, Port 0 and XTALA1/2 float,
XTALB1/2 active; normal operation returns on a normal reset and nothing else is documented as differing.

## 14. Reduced EMI mode (§22 p. 88)

An ordinary SFR bit, not a pin or lock: **`AUXR.AO`, 8Eh bit 0**, reset 0. Set → ALE is no longer
output during internal code execution but stays active internally for MOVX, MOVC and external
fetches, and the ALE pin is weakly pulled high while disabled. `AUXR.M0` (bit 5, MOVX RD/WR pulse 6
clock periods default vs 30) is not an EMI control; `AUXR.DPU` (bit 7) disables pull-ups. AUXR
reset prints `XX00 10'HSB.XRAM'0b` (Table 22-1) vs `0X00 1000b` (Table 3-11).

## 15. EEPROM data memory — AT89C51ED2 only (§23 pp. 89–92)

`EECON` D2h, reset `xxxx xx00b`, not bit-addressable: bits 7:2 reserved, bit 1 `EEE` (Enable
EEPROM Space), bit 0 `EEBUSY` (programming busy). 2048 bytes on-chip, mapped to **0000h–07FFh of
the XRAM/ERAM space**, accessed by `MOVX` while EEE=1; EEE=0 maps those addresses to XRAM.
- Write (Figure 23-1 p. 90): check EEBUSY → save/disable interrupts (if ISRs use XRAM) → DPTR =
  address → A = data → set EEE (EECON=02h) → `MOVX @DPTR,A` → clear EEE → restore interrupts;
  hardware sets EEBUSY for the duration and **clears it when programming ends**.
- Read: same frame but EEE set and `MOVX A,@DPTR`; byte-wise, no programming involved.
- `EEBUSY` cannot be written by software; while set the segment is unreadable/unwritable. Programming
  time is specified as **tWRITE = 7–17 ms, "Flash or EEdata programming time"** (§25 p. 115,
  2.7<VCC<5.5 V; ICCWRITE = 0.8×F(MHz)+15 mA). Erase granularity: `not specified in datasheet`.
- Gotcha: EEE remaps the *same* MOVX range between XRAM and EEPROM, so the array is chosen per
  access, not per address; mask interrupts since an ISR touching XRAM would hit EEPROM.
