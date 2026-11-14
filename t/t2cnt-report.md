# Timer 2 counter/capture edge counting — measured verdict

Scope: `~/s51` AT89C51RD2 ucsim fork. Nothing under `vendor/` was modified; the candidate
fix was built only in `/tmp/ucsim-t2cnt`. `V=/home/oleksiy/s51/vendor/ucsim-stc/ucsim/src/sims/s51.src`.
Suite: `/home/oleksiy/s51/t/t2cnttest.py` (30 legs, ~0.3 s, honours `S51_SIM`, exits
non-zero). Candidate diff: `/tmp/t2cnt-portcfg.patch`.

## 1. Verdict

**The model over-counts by exactly 2 in counter mode (`T2CON.C/T2=1`) whenever the count
input is exercised the way a program exercises it — by writing the port latch.** It does
not over-count when the pin is driven as an input from outside, and it never counts a
static level or a rising edge.

| stimulus | what the part must do | what the model does |
|---|---|---|
| falling edge via latch write (`MOV P1,#…` clearing P1.0) | +1 | **+2** |
| falling edge via external pin drive (`port_1_cfg` pin write) | +1 | +1 (correct) |
| rising edge only | +0 | +0 (correct) |
| repeated write of the same value (static level) | +0 | +0 (correct) |
| falling edge on T2EX (P1.1) | +0 to TL2 (it is the capture/direction pin) | +0 (correct) |

Same numbers on `-t 89C51RD2` and on `-t C52`, and identical on the pre-Phase-1 control
binary `/tmp/s51-ref-b`, so this is a **core/port-layer defect shared by every 51-family
device** — not something this project introduced, and not an RD2 device-file bug.

It affects both counter-capable Timer 2 modes (capture `CP/RL2#=1`, §12 p.31, and
auto-reload `CP/RL2#=0`, §12.1 p.31) and, through the same single root cause, Timer 0,
Timer 1 and the PCA ECI counter. `timer2.cc` is **not** at fault: its event handler and its
`cycles = T_edge` consumption are correct — it is simply handed two events per edge.

## 2. Measured matrix

Vendor binary `$V/ucsim_51` (every value identical on the control `/tmp/s51-ref-b`);
candidate value in the last column. "edge" = one falling transition produced by
`MOV P1,#0xFE ; MOV P1,#0xFF` style pairs, pins left at their reset value `0xff`.

| leg | expected (datasheet) | vendor | candidate |
|---|---|---|---|
| capture mode `T2CON=0x07`, 1 falling edge → TL2 | 1 | **2** | 1 |
| capture, 2 edges → TL2 | 2 | **4** | 2 |
| capture, 3 edges → TL2 | 3 | **6** | 3 |
| capture, 4 edges → TL2 | 4 | **8** | 4 |
| capture, 4 edges → TH2 | 0 | 0 | 0 |
| capture, 1 edge starting TL2:TH2 = `FFFF` → TL2,TH2,T2CON | 0,0,`0x87` (TF2) | **1**,0,`0x87` | 0,0,`0x87` |
| auto-reload `T2CON=0x06`, 1/2/3 edges → TL2 | 1/2/3 | **2/4/6** | 1/2/3 |
| `T2CON=0x0F` (EXEN2=1), only P1.0 moves → TL2,T2CON | 3,`0x0F` (EXF2 clear) | **6**,`0x0F` | 3,`0x0F` |
| `T2CON=0x0F`, P1.0 and P1.1 fall together → TL2,T2CON | 3,`0x4F` (EXF2 set) | **6**,`0x4F` | 3,`0x4F` |
| … same run, RCAP2L (capture latch of TL2) | 2 | **4** | 2 |
| `T2CON=0x03` (TR2=0), 3 edges → TL2 | 0 | 0 | 0 |
| 3 raising-only writes → TL2 | 0 | 0 | 0 |
| 4 identical writes (static level) → TL2 | 0 | 0 | 0 |
| `CLR P1.0` ×3 (1 edge + 2 static) → TL2 | 1 | **2** | 1 |
| `CLR`/`SETB P1.0` ×2 (2 falling) → TL2 | 2 | **4** | 2 |
| `C/T2=0`: two equal-length runs differing only in the pin writes → ΔTL2 | 0 | 0 | 0 |
| pin-drive route, 1/2/3 falling pin edges → TL2 | 1/2/3 | 1/2/3 | 1/2/3 |
| `-t C52` capture, 2 edges → TL2 | 2 | **4** | 2 |
| `-t C52` auto-reload, 2 edges → TL2 | 2 | **4** | 2 |
| T0 (P3.4) counter, 1 / 2 full toggles → TL0 | 1/2 | **2/4** | 1/2 |
| T1 (P3.5) counter, 1 / 2 full toggles → TL1 | 1/2 | **2/4** | 1/2 |
| T0 counter, no toggle at all → TL0 | 0 | 0 | 0 |
| PCA `CMOD=0x06` (CPS=11 → ECI=P1.2), `CCON=0x40` (CR), 2 falling ECI edges → CL | 2 | **4** | 2 |

Everything that over-counts over-counts by a factor of exactly two, on both devices and on
both counter-capable Timer 2 modes, and it is bit-path-independent: the byte latch write
and the bit-addressable `CLR`/`SETB` write are both affected.

Two independent confirmations of the count (not just repeated dumps of one register):

* `EXEN2` capture latches `TL2` into `RCAP2L`; the vendor binary reports `RCAP2L=4` after
  two edges (`2` on the candidate) — the second count had already landed when the capture
  ran.
* The `TF2` leg measures the overflow boundary instead of a magnitude: one edge from
  `FFFFh` must land on `0000h` with `TF2` set; the vendor binary lands on `0001h`.

## 3. Ground truth (doc4235K, AT89C51RD2/ED2, 4235K–8051–05/08)

* **§12 Timer 2, printed p.31:** *"C/T2 selects FOSC/12 (timer operation) or external pin
  T2 (counter operation) as the timer clock input. Setting TR2 allows TL2 to increment by
  the selected input."* Same page: *"Timer 2 operation is similar to Timer 0 and Timer 1."*
* **Table 12-1 T2CON, printed p.33–34, bit 1 `C/T2#`:** *"Set for counter operation (input
  from T2 input pin, **falling edge trigger**)."* — one count per falling edge, not two.
* **Table 12-1 bit 2 `TR2`:** *"Cleared to turn off Timer 2."* Bits 0/3/6 (`CP/RL2#`,
  `EXEN2`, `EXF2`): capture or reload happens on a *negative transition on the T2EX pin*
  when `EXEN2=1` — so an edge on P1.0 must never set `EXF2`, and an edge on P1.1 must never
  clock `TL2`.
* **Port 1 alternate functions, printed p.12:** *"T2 (P1.0): Timer/Counter 2 external count
  input/Clockout"*, *"T2EX: Timer/Counter 2 Reload/Capture/Direction Control"*, *"ECI:
  External Clock for the PCA"* (P1.2). **Port 3, printed p.13:** *"T0 (P3.4): Timer 0
  external input"*, *"T1 (P3.5): Timer 1 external input"*.
* **§12.1 Auto-reload Mode, printed p.31:** *"configures Timer 2 as a 16-bit timer or event
  counter with automatic reload"* — the counter input applies in reload mode as well.

**What exactly is counted:** falling edges on the T2 pin, P1.0. `T2EX` (P1.1) is a different
pin with a different job.

**What a latch write to a pin the part uses as an input means.** On silicon: nothing.
`P1.0` is the pin, and writing the P1 latch while the pin is driven externally neither
creates nor destroys an edge (the quasi-bidirectional cell only fights the driver). The
simulator has no independent wire, so it derives the pin level as `pins & latch` —
`cl_timer2::happen` `$V/timer2.cc:402-403`, and the identical idiom in
`$V/timer0.cc:401-402`, `$V/pca.cc:371-372`, `$V/interrupt.cc:173-174`. Under that
convention, a latch write that clears a bit the pins hold high **is** the falling edge and
must be worth exactly one count. `cl_port::set_pin` (`$V/port.cc:232-248`) implements the
other convention — a genuinely external drive — and it already counts 1:1. No reading of
the datasheet makes one latch write two counts.

## 4. Root cause

`cl_port::write` keeps the read-only `port_value` config mirror up to date with a
**device-visible write** to its own config cell:

* `$V/port.cc:164-165` — byte latch branch
* `$V/port.cc:177-178` — bit-addressable latch branch
* `$V/port.cc:246-247` — tail of `cl_port::set_pin`

`cl_hw::init` registers every config cell with its owning hardware element —
`core/sim.src/hw.cc:104`: `cfg->register_hw(a, this, false);` — so writing that cell goes
through `cl_hw_operator::write` (`core/sim.src/mem.cc:852-864`), which calls
`hw->write(cell, &val)`: **the same port's hook, re-entered one level deeper with the cfg
cell**. The inner pass skips the two mirror blocks (its `cell` is neither `cell_p` nor a bit
cell), runs `conf(cell,val)` → `cl_hw::conf` (`core/sim.src/hw.cc:160`) →
`cl_port::conf_op(port_value)` (`$V/port.cc:217-221`), and then — critically — falls through
to the event block at `$V/port.cc:184-189` and emits one `EV_PORT_CHANGED`. Control returns
to the outer pass, whose own `cell_p` has still not been stored (`cl_memory_cell::write`,
`core/sim.src/mem.cc:1242`, runs the operators *before* `d(val)`), and it emits the
**second** identical event. `cl_timer2::happen` (`$V/timer2.cc:394-412`) then faithfully
does `T_edge++` twice, and `do_t2_capture` / `do_t2_reload` (`$V/timer2.cc:243-244`,
`278-279`) hand `cycles = 2` to the loop that increments `TL2`.

Verified directly, not inferred. Instrumented copy at `/tmp/ucsim-t2cnt-dbg` (fprintf at
entry to `cl_port::write`, `cl_port::set_pin`, `cl_port::conf_op`, `cl_timer2::happen` and
`cl_timer2::do_t2_capture`, gated on `T2DBG=1`) prints, per `MOV P1,#0x00`:

```
[port1 ENTER] cell=<P1 cell>  cell_p=<P1 cell> depth=0
[port1 ENTER] cell=<cfg cell> cell_p=<P1 cell> depth=1
[port1 conf_op] addr=2 (=port_value) val=0x…
[port1] prev=0xff new=0x00 pins=0xff inform=1     <- inner pass
[happen] p1o=0xff p1n=0x00                        <- T_edge 0 -> 1
[port1] prev=0xff new=0x00 pins=0xff inform=1     <- outer pass
[happen] p1o=0xff p1n=0x00                        <- T_edge 1 -> 2
[cap] cycles->2 TL2=0
```

and per external pin drive exactly one `inform=1` with one matching `happen`, i.e.
`cycles->1`. The recursion stops at depth 1 because the inner pass's `cell` is not the
latch cell, so it never reaches the mirror write again.

The pin-drive path is numerically correct for a subtle reason: `set_pin` informs first (the
correct event) and *then* writes the mirror, so its spurious second event carries `pins`
already updated and yields `p1n == p1o` — no subscriber sees an edge in it. It is still a
wasted double event, and the fix removes it too.

## 5. Fix options and blast radius

The bug is one emit too many **at the source**, so the fix belongs in `port.cc`, not in
`timer2.cc`. Filtering in `cl_timer2::happen` would leave Timer 0/1 and the PCA doubled,
and a timer-side dedupe cannot even distinguish "same edge twice" from "two real edges in
one machine cycle" — the two events arrive with identical payloads. Wrong layer, and not
implementable there.

**Option A (chosen, 3 changed lines): update the mirror silently.** `cfg->write(port_value,
…)` → `cfg->set(port_value,…)` at `$V/port.cc:165`, `:178`, `:247`. `set()` stores without
notifying, so there is no re-entry and exactly one event per logical change.
`cl_port::init` already does exactly this at `$V/port.cc:105` (`cfg->set(port_value, prev &
cell_in->get())`), and the effect on the cell is bit-identical to the old path: the old
path's own `conf_op(port_value)` did `cell->set(*val)` and `cl_hw::conf` then did
`cell->set(*val)` again before the outer store. Diff: `/tmp/t2cnt-portcfg.patch`.

**Option B (alternative, also correct): refuse the event work for config cells** — at the
top of `cl_port::write`, `if (cfg && cfg->is_owned(cell, &a)) { conf_op(cell, a, val);
cell->set(*val); return; }`. Same one-event-per-change result; additionally immunises any
future `cl_port` subclass against re-entering the event block, at the cost of duplicating
`cl_hw::conf`'s body. Prefer A for the minimal diff; move to B if a subclass ever overrides
`cl_port::write` and re-adds a mirror write.

**Blast radius.** The only two `inform_partners(EV_PORT_CHANGED, …)` sites in the whole 51
model are `$V/port.cc:189` and `$V/port.cc:245`, and every 51-family device uses this
`cl_port` (`cl_stc12_port_mode::write`, `$V/stc12_port.cc:65`, is a documented no-op that
never informs). Subscribers, from a grep for `EV_PORT_CHANGED` handlers across `$V`:

| subscriber | pins | effect of the double event | after fix |
|---|---|---|---|
| `cl_timer2::happen` `$V/timer2.cc:399` | P1.0 `T2`, P1.1 `T2EX` | `T_edge` and `t2ex_edge` doubled → **TL2/TH2 count 2×** (measured); `EXF2`, capture latch idempotent | 1:1 |
| `cl_timer0::happen` `$V/timer0.cc:398` | P3.4 `T0`, P3.5 `T1` | `T_edge` doubled → **TL0/TL1 count 2×** (measured) | 1:1 |
| `cl_pca::happen` `$V/pca.cc:368` | P1.2 `ECI`, P1.3-7 `CEX0..4` | `ECI_edge` doubled → **PCA counter (`CPS=11`) counts 2×** (measured); `do_pca_capture` runs twice per edge latching the same `CH:CL`, `CCFn`/`CAPn`/`CAPP` flags idempotent | 1:1, captures unchanged |
| `cl_interrupt::happen` `$V/interrupt.cc:170` | P3.2 `INT0`, P3.3 `INT1` | `IE0`/`IE1` set twice (idempotent); `bit_INT0/1` recomputed to the same value | unchanged |
| `rd2_kbd`, `rd2_spi`, `rd2_brg` | — | deliberately do not subscribe (`$V/rd2_kbd.cc:142`, `$V/rd2_spi.cc:194`) | n/a |

Partner wiring for reference: `$V/port.cc:67-68` (port 1 → `HW_TIMER 2`, `HW_PCA 0`),
`$V/port.cc:82-85` (port 3 → timer0, timer1, interrupt, dummy). So the fix changes behaviour
for exactly the four counter paths above (all corrections) and is a no-op for flags,
captures and interrupts.

## 6. Candidate verification

Built in `/tmp/ucsim-t2cnt` (full `cp -r` of the vendor repo, `make -C
/tmp/ucsim-t2cnt/ucsim/src/sims/s51.src -j8`). The vendor tree was never written to, and the
patch is **not** applied there.

* `t2cnttest.py`: vendor **FAIL — 9/30 legs pass (21 bad)**, control `/tmp/s51-ref-b`
  **FAIL — 9/30 (21 bad)**, candidate **PASS 30/30**. Runtime 0.3 s.
* `t/floor.sh /tmp/ucsim-t2cnt/ucsim/src/sims/s51.src/ucsim_51` → **`floor: 0 suite(s) not
  green`** over all 20 suites (ident89c51r, rd2identity 38/38, rd2registers 288/288,
  banktest 3/3, ccapmrst 12/12, dpstest 2/2, t2oetest 7/7, wdttest 14/14, x2test 15/15,
  xramtest 10/10, hwtest 8/8, irctest 50/50, pcapin 21/21, euarttest, uartmp 19 checks/0
  failures, runtests at its 11/20 baseline, kbdtest 30, spitest, ckrltest, bootromtest).
* `ident89c51r`: **36/36 transcripts byte-identical**, candidate vs `/tmp/s51-ref-b`, so
  `-t 89C51R` output is untouched.
* The change touches neither rates nor flags: `t2oetest` (clock-out rate), `ckrltest`
  (prescaler), `irctest` (external interrupts) and `pcapin` (CEX pin ↔ module capture
  pairing) all stay green — evidence that removing the second event alters only the counter
  counts.

## 7. Control audit of the suite

With `S51_SIM=/tmp/s51-ref-b`, 9 of 30 legs pass. Each is a *negative* or *invariant* leg,
and each exists to stop the suite from being satisfied by a bogus fix:

1. `static level (4 identical writes) → 0` and `3 raising-only writes → 0` — a fix that
   counted writes instead of edges breaks these.
2. `T12-1 TR2: C/T2=1 with TR2=0 → 0` — pins `TR2` gating (Table 12-1 bit 2).
3. `p.12 pin table: T2EX(P1.1) edges do not clock TL2` — pins the pin identity; a fix that
   stopped counting by ignoring P1 writes wholesale, or widened the mask, breaks it.
4. `T12-1 C/T2#: C/T2=0 → pin edges add 0 counts` — differenced pair of equal-length runs,
   so nothing is measured from absolute elapsed counts.
5. `p.13: T0 counter, static P3 (no toggle) → 0`.
6-8. the three `pin-drive route` legs — these already pass on the unfixed binary **by
   design**: they are the 1:1 reference the latch route is measured against, and the
   tripwire for the tempting wrong fixes (suppressing latch-derived events, or deduping
   inside `timer2.cc`, which would leave the pin route unexplained and T0/T1/PCA doubled).

All 21 legs that measure the defect fail on the control binary.

## 8. Related observations (outside this assignment, not fixed)

* `cl_timer0::happen` (`$V/timer0.cc:403-404`) samples the T0/T1 counters on a **rising**
  edge while `cl_timer2::happen`, `cl_pca::happen` and `cl_interrupt::happen` all sample
  falling edges. doc4235 says Timer 2 operation is similar to Timer 0/1 (§12 p.31) and
  Table 12-1 pins Timer 2 on the falling edge, so the T0/T1 polarity deserves its own check
  against the Atmel hardware manual. My T0/T1 legs assert "one count per complete toggle",
  which holds under either polarity.
* Console pin drive is unusable for a chosen port: `cl_cmd_arg::as_hw`
  (`core/sim.src/arg.cc:341-357`) matches `id_string` as a *prefix*, so `set hardware port1
  …` answers `Error: no hw` and bare `port` is rejected as ambiguous. The working route is
  the config address space: `set memory port_1_cfg 1 <val>` (address 1 = `port_pin`).
* `cl_pca::print_info` prints `pca[0] FIXME`, and `info hardware timer` lists no elements
  for this device, so peripheral state has to be read out of SFRs.
* `CCON.CR` is bit 6 (`bmCR=0x40`), not bit 7 (bit 7 is `CF`) — easy to get wrong.

Scratch artefacts from this investigation (safe to delete): `/tmp/t2probe.py` (matrix
probe), `/tmp/ucsim-t2cnt-dbg` (instrumented copy used for the event-trace proof),
`/tmp/probe.hex`, `/tmp/t2c_*.hex`, `/tmp/t2x_*.hex`, `/tmp/pcaprobe.hex`,
`/tmp/t0probe.hex`, `/tmp/p1.hex`, `/tmp/floor-candidate.txt`, `/tmp/t2cnt_*.txt`.
