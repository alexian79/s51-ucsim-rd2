# AT89C51RD2/ED2 SPI — private-tree implementation and test suite

Tree: `/tmp/ucsim-spi` (copy of `~/s51/vendor/ucsim-stc`, nothing under the vendor tree
touched). Build: `make -C /tmp/ucsim-spi/ucsim/src/sims/s51.src -j8` →
`.../s51.src/ucsim_51` (clean, no warnings from the new files).
Suite: `/home/oleksiy/s51/t/spitest.py` — **56 checks, 0 failed, 0.4 s wall**
(`S51_SIM=... S51_REF=... S51_TIMEOUT=...` honoured; picks up `t/floor.sh` automatically).

## 1. Files

| file | what |
|---|---|
| `rd2_spiccl.h` | `cl_rd2_spi` declaration, pin map, "not modelled" list |
| `rd2_spi.cc` | the model + `cl_rd2_spi_it_src` (two `cl_it_src`s on `bmESPI`) |
| `regs51.h` | `bmSPR2/SPEN/SSDIS/MSTR/CPOL/CPHA/SPR1/SPR0`, `bmSPIF/WCOL/SSERR/MODF`, `bmSPIX2` (after `bmSRC`) |
| `objs.mk` | `rd2_brg.o rd2_spi.o` |
| `uc89c51r.cc` | `#include "rd2_spiccl.h"`; registered in `cl_uc89c51rd2::mk_hw_elements()` **only** (so the RB2/RC2 never see it) |

Names/reset values were already in `uc51.cc sfr_tab51` and
`cl_uc89c51rd2::clear_sfr()` (SPCON `0x14`, SPSTA/SPDAT `0x00`) — behaviour only was
added. Element: `HW_TIMER`, **id 14**, so `info hardware spi` works (the category is the
object name).

## 2. Model decisions, with doc4235K pages as printed

* **Clock**: `acc += cycles * fclk_periph_per_cycle()` (= 6 FCLK PERIPH periods per
  machine cycle in *both* clock states, §6/table 7-2), `half = BD/2` FCLK periods, so a
  bit is `BD` FCLK periods = `2*BD` oscillator clocks at 12 clocks/cycle and a byte is
  `16*BD`. The divider ladder is **table 16-3 continued on printed p. 70** (the row block
  spills off p. 69 — the classic spillover trap): `000..110 → /2,/4,/8,/16,/32,/64,/128`,
  `111 → Invalid`. §16.2's table 16-1 (p. 64) prints the same ladder with a different
  first row; p. 70's bit-table form is the one implemented.
* **SPR=111** → no divider, `half=0`, no edge ever, SPIF never set, byte stays loaded.
  Literal reading of "No BRG"/"Invalid"; a single line in `divider()` changes it if a
  real part is found to treat it as /256.
* **X2/SPIX2**: X2 halves the byte time in oscillator clocks (FCLK PERIPH becomes Fosc).
  `CKCON1.SPIX2` doubles the half period **only when `CKCON0.X2` is set** (table 7-2,
  p. 20: "when X2 is low, this bit has no effect") → X2+SPIX2 = the standard rate.
  Table 7-2's own wording ("6/12 clock periods per peripheral clock cycle") is not
  reconcilable with FCLK PERIPH being `fosc/2` and was not followed literally; the factor
  of two and its condition were.
* **Edges**: 16 half-periods/byte (§16.2.3 p. 64 "eight clock cycles"); edge 0 is by
  construction the edge that *leaves* the idle level, so `sample_edge()` needs only CPHA
  (CPOL never appears) and all four modes come out as the datasheet pictures. SPIF lands
  on the 8th sample: transition 15 with CPHA=0, 16 with CPHA=1; the master runs all 16 so
  SCK always parks on CPOL.
* **CPHA=0 drives MOSI at the SPDAT write** (figure 16-4 p. 67 "the first SCK edge is the
  MSB capture strobe… the Slave must begin driving its data before the first SCK edge");
  **CPHA=1 waits for the first edge** (figure 16-5).
* **Flags**: SPSTA writes are dropped (`*val = cell value`) — all four bits are
  set/cleared by hardware and every clearing mechanism in §16.3.3 is a *sequence* on
  other registers. SPIF/WCOL: SPSTA read (arms) then SPDAT read *or* write. MODF: SPSTA
  read then a **write to SPCON** (§16.3.3.1); it is evaluated as a **level** each master
  tick, because footnote 1 p. 64 ("Clearing SSDIS control bit does not clear MODF") is
  only meaningful that way — and it therefore re-asserts while SS is still low, so
  software must free the pin or set SSDIS. SSERR: SS rising mid-byte with
  `captures > 0`; requests nothing; cleared **only** by SPEN 1→0.
* **WCOL**: set on a SPDAT write while running as master, the colliding byte is
  **discarded** (not swapped in) and the transfer continues (§16.3.3.2 "continues
  uninterrupted"), no interrupt.
* **SPDAT read answers the receive buffer** to every reader, including the console and
  any other registered operator (the cell itself is set to `rx_buf` on write) — p. 71.
* **MODF side effects**: `SPEN` and `MSTR` cleared via the cell's `set()`, transfer
  dropped.
* **Slave**: SS = P1.1, SCK = P1.6, MOSI = P1.7, MISO = P1.5 (table 4-1 pp. 11-12),
  edges classified by the same CPHA rule; SS falling re-aligns the byte only with CPHA=0
  (figure 16-6 p. 67, footnote 2 p. 64); `SSDIS` frees SS from selection only with
  CPHA=1 (table 16-3 bit 5); SS high → MISO left alone (the Hi-Z of §16.2.4 has nothing
  to be in a latch cell).
* **P1 is shared** (T2/T2EX, ECI, CEX0..CEX4): every SPI pin write is a read-modify-write
  of the P1 cell done with `set()`, never bit 1, re-asserted from the model's own levels
  each master tick. A consequence of `set()`: no `EV_PORT_CHANGED`, so SPI traffic cannot
  trigger a PCA capture on CEX3/CEX4 — deliberate.
* **Interrupt**: two `cl_it_src`s on `IEN1`/`bmESPI`, vector `0x004b` — `SPIF` →
  "SPI transmitter", `MODF` → "SPI receiver/error" (figure 16-7 p. 69, table 17-2 p. 73).
  `cl_rd2_spi_it_src::enabled()` tests `bmESPI` **alone**, because the base
  `enabled()` ANDs the whole `ie_mask` (which carries the pair marker) with `IEN1`, so
  `MOV IEN1,#0x84` would silently switch the SPI off. `spitest` has a leg for exactly
  that, and it fails if the override is removed.
* **Inherited contradictions**: SPSTA/SPDAT reset values are printed `0000 0000b` in one
  place and `0001 0100b` in another (SIMULATION-PLAN §3.2) — I kept the values already in
  `clear_sfr()` (SPCON `0x14`, SPSTA/SPDAT `0x00`) and added nothing. SPCON `0x14` itself
  decodes as MSTR+CPHA under table 16-3's bit layout while the prose says "disabled but
  still configured as Master"; the cell keeps `0x14` either way, so no behaviour depends
  on resolving it.

## 3. Four real defects the suite found, all fixed in this tree

1. **SS edge branches were inverted against their own comments**: `prev_ss && !ss_low`
   (SS *rising*) was running the "SS falling → realign" body and vice versa, so SSERR was
   never raised and CPHA=0 byte alignment happened on release. Fixed in `tick()`.
2. **`SPEN` 0→1 did not force SCK to CPOL**: it called `sck_level()`, which early-returns
   when the requested level equals the cached one; the cache starts low, so a latch left
   high by the program kept SCK high while the master believed it idled at 0. Now the
   enable drives unconditionally.
3. **CPHA=1 drove MOSI before the first SCK edge** (the per-cycle re-assertion was
   unconditional). Now gated on `edge || !CPHA`, per figure 16-5.
4. **`print_info`'s last line printed the event counters under the keys `WCOL=`,
   `MODF=`, `SSERR=`**, i.e. the same keys as the flag line above it; any key=value reader
   silently got the counters, so a cleared MODF still read "MODF=1". Renamed to
   `bytes= wcol_events= modf_events= sserr_events=`. **Interface change worth knowing for
   `t/ident89c51r.py`-style guards and any doc that quotes the `info hardware spi` block.**

Plus one defect in the *suite* worth remembering: a relative jump whose target is computed
from `len(code)` before the rest of the sequence is appended aims at itself — the poll
loop became `MOV A,SPSTA / ANL A,#SPIF / JZ <itself>`, which spins forever and made every
timing leg measure a spin. `poll_spif()` is now the only place those offsets exist.

## 4. Probe order (stated because it changes results)

Every leg does **`info hardware spi` first, `dump sfr` afterwards, in one console
session**. Reading SPSTA arms the clear sequence and reading SPDAT performs it, so
`dump sfr 0xc3 0xc5` consumes SPIF/WCOL/MODF; `info hardware spi` reads the same cells
through `get()` and disturbs nothing. The two dumps the suite makes after the instrument
(P2 marker, T2CON) are not SPI registers. Two legs replay the *same image* at two `step`
counts (MODF live vs cleared, CPOL idle vs complete) instead of poking state.
`clocks()` uses `break`+`go`+`state` and is wrapped in a subprocess timeout
(`S51_TIMEOUT`, default 30 s) that names the leg — a leg whose transfer never completes
would otherwise wedge the floor. `step N` is used everywhere a transfer may never finish.

Timing method: two runs of one program differing only in byte count, divided by the byte
difference (launch cost cancels). The loop around a byte costs a fixed 8 machine cycles
(96 clocks) plus up to one poll pass (48), so the STD window is `16*BD … 16*BD+144`
(X2: `+72`). The overhead-free assertion is the **cross-divider difference**:
`per_byte(BD=128) − per_byte(BD=2) = 16*(128−2) = 2016 ± 48` (measured 2016.0).

## 5. Control-binary audit (`S51_SIM=/tmp/s51-ref-b`, no SPI at all)

`Checks run: 53 Failed: 47` in ~3 min (legs that wait for SPIF hit the 4 s console
timeout). Legs that PASSED there:

* the four `-t 89C51R` absence legs — **expected to pass on the control**; that is the
  point of the control (they assert absence, so a binary without the model satisfies them
  by construction). They are paired with an RD2 leg that asserts the same three addresses
  *are* owned and decodable here (`SPEN/MSTR/CPHA` decode, `BD=2`, `bytes=1`), which no
  SPI-less binary can produce.
* "SSERR requests nothing" (`marker=0`) — a negative check; its positive partner
  "SS released mid-byte sets SSERR" fails on the control, so the pair is sound.
* "SPDAT reads back the received byte 0xff" — **was vacuous** (Main's failure mode: the
  poll loop never exits, the move to the marker port never runs, P2 keeps its reset
  `0xff`, which is the value looked for). Fixed: the leg now also requires the model's
  `bytes == 1`, so it cannot pass without a real transfer. Re-verified on the RD2
  (56/56) after the change; on the control it now fails by construction.

## 6. Absence guard, and why it is written the way it is

`info hardware` with no category, and `info hw <category>` for a category the CPU lacks,
both print **only** the usage line — so a byte-diff "over the hardware list" compares two
identical usage strings and can never fail. `ident89c51r.py` covers `info hw wdt`/`info hw
pca` only and will **not** see an SPI element leaking onto the AT89C51RB2/RC2. The suite
therefore proves absence behaviourally, under `-t 89C51R` on this tree: no `spi[` line
(byte-identical to the control's usage line, asserted), the three addresses read `0x00`
after being written (on that part 0xc3/0xc4/0xc5 are not registers at all — note they
yield *no cell*, which is why the assertion accepts `None` or `0`), Port 1 never moves,
and the 004Bh routine never runs.

## 7. For the cutover

* **`priority_of()` and `IP_PAIR1`**: in this copy `IP_PAIR1` is still file-static in
  `uc51.cc:2180` and the pair-1 arm is `l = sfr->get(IPL1) & nuof_it` **without the bit-7
  strip** Main describes. `rd2_spi.cc` has an `#ifndef IP_PAIR1` fallback guard that goes
  inert once `regs51.h` defines it, so the source will land on the stripped arm after the
  cutover; if the strip is not present, `MOV IPL1,#0x80` hands this source a level. No
  leg of `spitest` sets IPL1/IPH1 bit 7, so nothing here hides the difference — but the
  comment at `uc51.cc:2198` ("Nothing sets IP_PAIR1 yet, because the keyboard and the SPI
  have no cl_it_src") becomes stale the moment this is merged and should be updated.
* **Baselines**: `test/config/Makefile` still has no 89C51RD2 baseline (the gap opened by
  the BRG); any `add_hw` invalidates `test/config/baseline/89C51R`, and this add is
  RD2-only, so the 89C51R baseline should be untouched — `t/ident89c51r.py` plus
  `t/floor.sh` are the guards, and `spitest.py` is now in the floor's glob.
* **Not modelled** (documented in `rd2_spiccl.h`): maximum slave SCK frequency (§16.3.1.2
  says `FCLK PERIPH/4`, footnote 3 on the same page says bus clock — irreconcilable, and
  the clock is an asynchronous pin sampled once per machine cycle anyway); electrical
  Hi-Z on MISO; a SPDAT transmit buffer (there is none in the part); the
  `SPSTA/SPDAT` reset-value contradiction (left as found).
* Files to copy: `rd2_spi.cc`, `rd2_spiccl.h`; hunks in `regs51.h`, `objs.mk`,
  `uc89c51r.cc`. `git diff` in `/tmp/ucsim-spi` shows them; no git writes were made there.
