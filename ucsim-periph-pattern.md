# Adding an on-chip peripheral to the 8051 model — ucsim-stc fork

Source-only recipe, derived by reading `/home/oleksiy/s51/vendor/ucsim-stc/`.
All paths below are relative to `ucsim/` unless prefixed `../` (the fork wrapper repo).
Line references were read in this tree; nothing was modified.

Worked examples used throughout: `stc12_brt.cc`/`stc12_brtcl.h` (baud-rate timer — the closest
structural analogue of an enhanced BRG), `stc12_adc.cc`, `stc12_wdt.cc`, `stc15_timer2.cc`,
`pca.cc`, `timer0.cc`, `wdt.cc`, `serial.cc`, plus the fork's own RD2 work in `uc89c51r.cc`.

---

## 0. The minimal file set (what a new peripheral actually touches)

| # | File | Change | Anchor |
|---|------|--------|--------|
| 1 | `src/sims/s51.src/<periph>cl.h` | new `class cl_<periph>: public cl_hw` | pattern: `src/sims/s51.src/stc12_brtcl.h:17-29` |
| 2 | `src/sims/s51.src/<periph>.cc` | ctor/`init`/`added_to_uc`/`write`/`tick`/`reset`/`print_info` | pattern: `src/sims/s51.src/stc12_brt.cc:27-121` |
| 3 | `src/sims/s51.src/regs51.h` | `#define` SFR addresses + `bm*` bit masks | `src/sims/s51.src/regs51.h:67-70`, `:241-257`, `:366-414` |
| 4 | `src/sims/s51.src/objs.mk` | add `<periph>.o` to `OBJECTS` | `src/sims/s51.src/objs.mk:5-12` |
| 5 | the device's `mk_hw_elements()` | `new` + `init()` + `add_hw()` | `src/sims/s51.src/uc89c51r.cc:63-73`, `src/sims/s51.src/ucstc12.cc:396-557` |
| 6 | `src/sims/s51.src/uc51.cc` `sfr_tab51[]` / `bit_tab51[]` | human-readable SFR/bit names | `src/sims/s51.src/uc51.cc:67`, `:198-200`, `:381` |
| 7 | device `clear_sfr()` / `reset()` | per-SFR reset values | `src/sims/s51.src/uc89c51r.cc:197-224` |
| 8 | `src/core/utils.src/globals.cc` `cpus_51[]` | only for a new/renamed *device*, not a peripheral | `src/core/utils.src/globals.cc:344-415` |
| 9 | `src/sims/s51.src/test/<name>/` or `../tests/` | test + baseline | `src/sims/s51.src/test/banker/Makefile:1-7`, `../tests/smoke.sh` |
| 10 | `src/sims/s51.src/test/config/baseline/*` | regenerated (hw count changes) | `src/sims/s51.src/test/config/Makefile:1-6`, `test/config/baseline/89C51R:6` |

Steps 1-7 are the real minimum for a new peripheral on an existing device.

---

## 1. `cl_hw` subclass: construction and registration

### 1.1 The base contract
`class cl_hw` is declared in `src/core/sim.src/hwcl.h:63-131`; the members a peripheral
overrides are:

```
cl_hw(class cl_uc *auc, enum hw_cath cath, int aid, const char *aid_string);  // hwcl.h:85
virtual int init(void);                                    // hwcl.h:88
virtual unsigned int cfg_size(void);                       // hwcl.h:89  (default 1)
virtual class cl_hw *make_partner(enum hw_cath, int id);   // hwcl.h:94
virtual t_mem read(class cl_memory_cell *cell);            // hwcl.h:96
virtual void write(class cl_memory_cell *cell, t_mem *val);// hwcl.h:97
virtual class cl_memory_cell *register_cell(...);          // hwcl.h:109-111 (3 overloads)
virtual int tick(int cycles);                              // hwcl.h:114
virtual void reset(void);                                  // hwcl.h:116
virtual void happen(class cl_hw*, enum hw_event, void*);   // hwcl.h:117-118
virtual void inform_partners(enum hw_event, void *params); // hwcl.h:119
virtual void print_info(class cl_console_base *con);       // hwcl.h:129
```

`enum hw_cath` (`src/core/utils.src/stypes.h:415-431`): `HW_TIMER`, `HW_UART`, `HW_PORT`,
`HW_PCA`, `HW_INTERRUPT`, `HW_WDT`, `HW_CALC`, … There is no `HW_SPI`; the fork reuses a
category rather than adding one — `cl_stc12_adc` passes `HW_TIMER /* reuse category */, 10`
(`src/sims/s51.src/stc12_adc.cc:31`). Adding a value to `hw_cath` is possible but touches a
core header; every fork peripheral so far reuses an existing category.

The `id` argument is a **global per-category discriminator that other peripherals switch on**.
Live ids on the s51 bus: timers 0/1/2 → `cl_timer0(this,0,"timer0")`, `cl_timer1(...,1,...)`,
`cl_timer2(...,2,"timer2")` (`src/sims/s51.src/uc51.cc:977-983`, `src/sims/s51.src/uc52.cc:64`);
BRT = 3 (`src/sims/s51.src/stc12_brt.cc:28`); ADC = 10 (`stc12_adc.cc:31`); WDT = 11
(`stc12_wdt.cc:23`); STC15 Timer2 = 12 (`stc15_timer2.cc:24`). The consumer reads them raw:
`serial.cc:366-397` dispatches on `where->id == 1|2|3|12`. **Pick an unused id and document it
in the consumer**, otherwise you silently re-route somebody's baud clock.

Constructor body (`src/core/sim.src/hw.cc:47-71`) sets `flags= HWF_INSIDE`, copies `id_string`
into `set_name()` (this is the name the debugger uses), allocates the `partners` list, and
initialises `cfg= 0`, `io= 0`, `active= false`. It does **not** set `on`, and it does **not**
create the cfg space — that is `cl_hw::init()`.

### 1.2 Registration in the device class
`cl_uc::mk_hw_elements()` is called from `cl_uc::init()` right after `make_cpu_hw()`
(`src/core/sim.src/uc.cc:643-644`); base implementation `src/core/sim.src/uc.cc:811-818`
(only `simif` + `vcd`). Each device appends its own peripherals by chaining up:

```
cl_uc89c51r::mk_hw_elements(void)          // src/sims/s51.src/uc89c51r.cc:63-73
{
  class cl_hw *h;
  cl_uc52::mk_hw_elements();               // timers 0/1, serial, ports, irq, timer2
  add_hw(h= new cl_wdt(this, 0x3fff)); h->init();
  add_hw(h= new cl_pca(this, 0));          h->init();
}
```

Conditional per-part registration (`has_adc`, `has_timer2`, `has_pca`, `pca_modules`) is done
exactly once, in `cl_uc_stc12::mk_hw_elements()`
(`src/sims/s51.src/ucstc12.cc:396-557`; BRT block at `:445-452`, ADC at `:530-535`,
WDT at `:537-540`, STC15 T2 at `:542-547`, PCA at `:549-555`).

`cl_uc::add_hw()` (`src/core/sim.src/uc.cc:2341-2360`) does **not** call `added_to_uc()`.
`added_to_uc()` fires from `cl_hws::add()` — `src/core/sim.src/hw.cc:611` — i.e. from
`hws->add(hw)`. Since `add_hw()` calls `hws->add()`, the effective order for your element is
`operator new` → your `init()` → `add_hw()` → `new_hw_adding/new_hw_added` on everyone else →
your `added_to_uc()`.

`cl_uc::init_add_hw()` (`src/core/sim.src/uc.cc:2361-2369`) is the `init(); add_hw();`
shorthand; the s51 tree calls the two steps explicitly instead (all of `uc51.cc:968-1043`,
`uc89c51r.cc:63-73`), so follow that style.

Registration order does **not** matter for partner wiring: `make_partner()` resolves eagerly via
`uc->get_hw(category, id, 0)` (`src/core/sim.src/hw.cc:657-667`), and a partner that does not
exist yet is bound later by `cl_hw::new_hw_added()` → `cl_partner_hw::refresh(new_hw)`
(`src/core/sim.src/hw.cc:117-129` + `:695-711`), which matches on `category` **and** `id`.

### 1.3 TRAP: `init()` must chain to `cl_hw::init()` first
`cl_hw::init()` (`src/core/sim.src/hw.cc:78-117`) is what sets `on= true` (`hw.cc:84`), creates
the hidden `<id_string>_<id>_cfg` address space + chip (`hw.cc:89-105`) and registers the
`<id_string><id>_on` variable (`hw.cc:107-111`). Every peripheral in the tree chains to it —
`pca.cc:55`, `wdt.cc:50`, `interrupt.cc:52`, `mdu.cc:167`, `port.cc:57`, `timer0.cc:93`,
`uc390hw.cc:48`, `stc12_adc.cc:43`, `stc12_port.cc:52`, `stc12_wdt.cc:33`,
`stc15_timer2.cc:34`, `uc51.cc:2238` — **except** `cl_stc12_brt::init()`
(`src/sims/s51.src/stc12_brt.cc:37-50`), which never calls it. Consequence: `hw->on` is read
uninitialised by `conf` (`src/core/cmd.src/cmd_conf.cc:74`) and gated on in the tick loop
(`src/core/sim.src/uc.cc:2998-3001`), and BRT has no `stc12_brt3_cfg` space and no
`stc12_brt3_on` variable. Do not copy that line of the example; copy the ADC one.

---

## 2. Getting SFR cells so a write reaches the model

### 2.1 The two acquisition calls
Inside `init()`:

```
class cl_address_space *sfr= uc->address_space(MEM_SFR_ID);   // "sfr" — stypes.h:331
cell_auxr = register_cell(sfr, AUXR);   // ← model gets callbacks
cell_brt  = sfr->get_cell(BRT_ADDR);    // ← model only READS/WRITEs the cell, no callback
```
`src/sims/s51.src/stc12_brt.cc:37-50` shows both side by side; `stc12_adc.cc:42-59`,
`stc15_timer2.cc:33-49`, `stc12_wdt.cc:30-37` and `pca.cc:51-82` follow the same rule:
**control/status registers you must react to → `register_cell()`; data registers you push
values into → `get_cell()`.**

Mechanism: `cl_hw::register_cell(mem, addr)` (`src/core/sim.src/hw.cc:236-244`) calls
`mem->register_hw(addr, this, false)`; the cell overload (`hw.cc:270-278`) calls
`cell->add_hw(this)` (`src/core/sim.src/mem.cc:1455-1460`), which appends a
`cl_hw_operator` (`mem.cc:1913-1917`). Every read/write through that cell then runs
`cl_hw_operator::read()/write()` (`src/core/sim.src/mem.cc:817-863`), which sets
`hw->active= true` around the call (recursion guard) and invokes your
`cl_hw::read(cell)` / `cl_hw::write(cell, &val)`.

There is **no** `mem_cell_changed` in current use — it survives only as commented-out dead
code in `timer0.cc:188-203`, `pca.cc:188-200`, `interrupt.cc:132-136`. Ignore it.

### 2.2 `write(cell, val)` semantics
* `*val` is the value about to be stored, and the value returned by
  `cl_hw_operator::write()` is what lands in the cell (`src/core/sim.src/mem.cc:852-863`).
  So you can auto-clear bits by editing `*val`: `cl_stc12_wdt::write()` clears `CLR_WDT` with
  `*val &= ~WDT_CLR_WDT` (`src/sims/s51.src/stc12_wdt.cc:39-52`).
* Alternatively write the cell yourself from inside the callback: ADC clears `ADC_START` and
  sets `ADC_FLAG` on completion with `cell_adc_contr->set(...)`
  (`src/sims/s51.src/stc12_adc.cc:128`).
* `write()` is called for *every* write to that cell, including `set mem sfr 0xbc 0x8b` from
  the command line (this is exactly how `../tests/smoke.sh` drives the model), and including
  writes your own model makes unless you use `cell->set()` instead of `cell->write()`.
  Compare `uc51.cc:1754-1759` (`cl_51core::clear_sfr` zeroes every SFR with `sfr->set(i,0)`,
  deliberately bypassing hooks) with `uc89c51r.cc:225-229` (the reset-time
  `sfr->write(CCAPMn, ...)`, deliberately going *through* them, because `cl_pca` keeps a
  private copy of `CCAPMn` that only `write()` refreshes). Driving someone else's pin is the
  opposite case and uses `set()` on purpose: `pca.cc:308,318,320` and `timer2.cc:385-387`
  set `P1` without re-entering the port's callbacks.
* **A peripheral's `reset()` runs before the SFRs are cleared.** `cl_51core::reset` calls
  `cl_uc::reset` (`uc51.cc:1739`), which loops `hw->reset()` over `hws`, and only then
  `clear_sfr()` (`uc51.cc:1741`). So anything cached from an SFR must be filled in `reset()`
  from the register's **reset constants** — reading the cell back gets the *previous*
  program's bytes, and not filling it at all leaves allocator garbage. That is how `cl_pca`
  ended up unable to count out of reset: its `clk_source` copy was never initialised and
  usually looked like the Timer-0-overflow clock (`pca.cc:326-342`).
* Dispatch is by pointer identity against your stored cells
  (`timer0.cc:150-186`, `pca.cc:129-186`, `stc12_adc.cc:62-86`). Loop over arrays when the
  peripheral is replicated (`pca.cc:158-185`).
* **Do not override `read()` unless you must.** The default `cl_hw::read()` calls `conf()`
  (`src/core/sim.src/hw.cc:145-158`), which is what makes the `<name>_on` cfg switch and any
  `conf_op()` override work. Every fork peripheral overrides only `write()`, leaving `read()`
  alone. If you do override `read()`, delegate: `cl_hw::read(cell)` (see
  `src/core/motorola.src/cia.cc:152-160` for the serial-style pattern).
* Bit-addressable SFRs live in the separate `"bits"` address space: register the whole byte
  **and** the bit cell — `cl_timer0::init()` registers `TCON` plus all 8 bits
  (`timer0.cc:110-117`) and `write()` reassembles the byte from the bit offset
  (`timer0.cc:150-165`); `cl_pca::init()` registers `CR` as bit cell `0xda`
  (`pca.cc:80-81`) and handles it in `write()` (`pca.cc:145-148`); `cl_interrupt::init()`
  registers `0x88`/`0x8a` (`interrupt.cc:58-61`). `RD2`'s SPI status register is
  bit-addressable, so you will need this.

### 2.3 Declaring the registers
Addresses and masks are plain `#define`s in `src/sims/s51.src/regs51.h`:
generic/RD2 at `:42-70` (`AUXR 0x8e`, `AUXR1 0xa2`, `CKCON0 0x8f`, `CKCON1 0xaf`),
AUXR1 masks at `:241-245`, CKCON0 masks at `:247-257`, STC12 block at `:366-398`,
STC15 delta at `:400-414`, ADC masks from `:416`. Add your SPI defines here; keep the
datasheet/table citation in the comment, as the RD2 block does (`regs51.h:247-249`).

---

## 3. Getting time

### 3.1 The contract, and what does not exist
There is **no event queue for peripherals** in this tree. `grep event_after` finds nothing;
`enum hw_event` (`src/core/utils.src/stypes.h:433-440`: `EV_OVERFLOW`, `EV_PORT_CHANGED`,
`EV_T2_MODE_CHANGED`, `EV_CLK_ON`, `EV_CLK_OFF`) is a *point-to-point notification*, not a
scheduler. `cl_hw::happen()` / `inform_partners()` are the only cross-peripheral signalling,
and `cl_hw` has no `hw_event`/`event_after` scheduling helper at all. (`cl_uc::events`
exists but is for breakpoints/`cl_brk`, `src/core/sim.src/mem.cc:881-904`.)

Time arrives exclusively through `virtual int tick(int cycles)`:

* `cl_uc::tick_hw(int cycles)` (`src/core/sim.src/uc.cc:2981-3005`) loops `while (c-- > 0)`
  and calls `hw->tick(1)` once per machine cycle for every hw with
  `flags & HWF_INSIDE && hw->on` — **your `tick()` always receives `cycles == 1`**.
* Callers: `cl_uc::post_inst()` → `tick_hw(inst_ticks)`
  (`src/core/sim.src/uc.cc:3341-3350`), `cl_uc::post_emu()`
  (`src/core/sim.src/uc.cc:3353-3359`), and the debugger command `tick [n]`
  (`src/core/cmd.src/cmd_uc.cc:258-272`, which calls `uc->tick(cycles)` then
  `uc->tick_hw(cycles)`).
* `cl_uc::tick()` (`src/core/sim.src/uc.cc:3016-3035`) is a *different* thing: it advances
  wall-clock counters and accumulates `inst_ticks`; it does not touch hws.
* Return value convention: everyone returns `resGO` (base returns 0,
  `src/core/sim.src/hw.cc:292-297`); nothing in `tick_hw` inspects it. Use `resGO`.
* A device-wide override hook exists: `cl_uc::tick_hw()` is virtual and
  `cl_uc_stc12` overrides it to feed an oscillator-clock trace accumulator
  (`src/sims/s51.src/ucstc12.cc:815-831`, declared `ucstc12cl.h:98`); `cl_uc320` also overrides
  it (`uc320cl.h:41`). `cl_uc::do_extra_hw()` (`src/core/sim.src/uc.cc:3007-3009`) is a no-op
  hook called after the loop.

Because `tick()` sees one machine cycle at a time, `clock_per_cycle()`
(`src/sims/s51.src/uc51cl.h:110`, overridden for X2 at `uc89c51r.cc:113-119`) is the way to
convert cycles → oscillator clocks, and it is *not* used to scale `tick()` — see
`uc89c51r.cc:100-111` for the rationale.

**TRAP: a divisor must be in the same unit as the accumulator it is compared against, and one
in-tree site violates it.** `tick_hw` hands you machine cycles, so a member that accumulates
`cycles` alone counts machine cycles, while `cycles * uc->clock_per_cycle()` counts oscillator
clocks (this is what `cl_serial` does: `s_tr_tick += cycles * uc->clock_per_cycle()`,
`src/sims/s51.src/serial.cc:441`). Comparing an oscillator-clock accumulator against a
machine-cycle-sized divisor makes every rate `clock_per_cycle()`× too fast, silently, and only
on the 12-clock parts. `serial.cc:279`
(`uc->clock_per_cycle() == 1 ? 16 : 32`) is exactly that bug — right for the 1-clock STC parts,
12× short for everything else (SIMULATION-PLAN.md D11). When you write a divisor, say in a
comment which unit it is in; when you read a rate bug, check the units of both sides first.

### 3.2 Carrying a sub-cycle / phase remainder — four in-tree idioms
1. **Modulo prescaler member.** BRT in 12T mode: `prescaler += cycles; effective= prescaler/12;
   prescaler %= 12;` (`src/sims/s51.src/stc12_brt.cc:74-79`). Same in
   `stc15_timer2.cc:66-73` and `stc12_wdt.cc:66-72` (accumulator + `while (n >= divisor)`).
2. **Countdown to a deadline.** ADC conversion latency:
   `conversion_delay -= cycles; if (<= 0) complete` (`src/sims/s51.src/stc12_adc.cc:90-96`,
   latency table at `:70-73`). Note the honesty comment: with `tick(1)` per *machine* cycle,
   "420 clocks" is really 420 ticks — say so in the comment.
3. **Per-peripheral carry owned by the CPU**, needed when the *CPU* clock changes under the
   peripheral (RD2 X2). `cl_51core::peripheral_cycles(int cycles, int x2_bit)`
   declared `src/sims/s51.src/uc51cl.h:111-117` (default identity), implemented
   `src/sims/s51.src/uc89c51r.cc:122-141` with `int x2_carry[7]`
   (`uc89c51rcl.h:62-64`, indexed by bit position, `total & 1` held back, `total >> 1`
   returned, carry *zeroed* whenever the half-rate condition is not active). Consumers:
   `timer0.cc:206-215`, `timer2.cc:149-150`, `pca.cc:200-216`
   (only the machine-cycle-derived PCA source — `pca.cc:213-215` comment), `wdt.cc:76-80`.
   **This is the mechanism to use for any new RD2 peripheral with its own CKCON0/CKCON1 bit.**
4. **Bit-phase accumulators owned by the consumer.** `cl_serial` keeps `s_tr_tick`,
   `s_rec_tick`, `s_tr_t1`, `s_rec_t1`, drains them `_divby` at a time
   (`src/sims/s51.src/serial.cc:249-292`, `_divby` chosen via `clock_per_cycle()` at `:266-268`,
   accumulation at `:337-340`) and zeroes them all in `reset()`
   (`src/sims/s51.src/serial.cc:349-364`). A timer that feeds it does not keep baud phase —
   it just pings: `stc12_brt.cc:95-101` → `inform_partners(EV_OVERFLOW, 0)`, delivered by
   `cl_hw::inform_partners` (`src/core/sim.src/hw.cc:299-309`) →
   `cl_partner_hw::happen` (`hw.cc:713-717`) → `cl_serial::happen`
   (`src/sims/s51.src/serial.cc:366-397`, which checks `where->id` and re-checks
   `AUXR.S1BRS` so only the selected baud source counts). Declare the partnership in your
   `init()` with `make_partner(HW_UART, 0)` (`stc12_brt.cc:46-47`, `stc15_timer2.cc:47-48`;
   fan-out example: `timer0.cc:78-82` registers five PCA partners).

Sleep/IDLE gating: check `uc->state == stIDLE` and the peripheral's own "stop in idle" bit —
`pca.cc:207-209` (`bit_CIDL`), `stc12_wdt.cc:59-61` (`IDLE_WDT`).

`reset()` MUST zero every remainder: `pca.cc:321-328`, `wdt.cc:91-96`,
`serial.cc:349-364`, `uc89c51r.cc:212-214` (`x2_carry` cleared because "any half machine cycle
held back is void now").

---

## 4. Raising an interrupt

### 4.1 `cl_it_src` construction
Declaration `src/core/sim.src/itsrccl.h:55-82`, definition `src/core/sim.src/itsrc.cc:47-80`:

```
cl_it_src(cl_uc *Iuc, int Inuof,                      // poll/numbering id, also HW id 100+Inuof
          class cl_memory_cell *Iie_cell, t_mem Iie_mask,   // enable register + mask
          class cl_memory_cell *Isrc_cell, t_mem Isrc_mask, // request(flag) register + mask
          t_addr Iaddr,                               // vector address
          bool Iclr_bit,                              // clear flag when the IT is accepted
          bool Iindirect,                             // addr is a vector table base
          const char *Iname, int apoll_priority);     // name (set_name) + poll order
```
`cl_it_src::init()` (`src/core/sim.src/itsrc.cc:84-89`) `register_cell()`s both the enable cell
and the flag cell, so the source re-evaluates `enabled()`/`pending()` straight from memory.
`ie_value`/`src_value` default to the masks (`itsrc.cc:66-70`; setters `itsrccl.h:73-74`).

### 4.2 Where they get created
Always from your `added_to_uc()` override, because the SFR cells and `uc->it_sources` must
already exist:

* `cl_interrupt::added_to_uc()` — `src/sims/s51.src/interrupt.cc:65-124`. External 0/1 at
  `:73-84`; PCA sources at `:85-119`, the interesting trick being a peripheral with **no
  global enable bit**: it uses the *per-module* `CCAPM0` cell with mask `0x01` as the `ie_cell`
  and `CCON` bit as the flag cell, three separate sources sharing vector `0x003B`
  (comment at `interrupt.cc:85-92` records that IE bit 6 is ELVD, not EC).
* `cl_timer0::added_to_uc()` — `src/sims/s51.src/timer0.cc:120-142` (`IE`/`ET0`, flag `TCON`/`TF0`,
  vector `0x000b`, `clr_bit= true`, poll priority 2/4).
* `cl_serial::added_to_uc()` — `src/sims/s51.src/serial.cc:111-117`.
* `cl_pca::added_to_uc()` — `src/sims/s51.src/pca.cc:85-127`, and it uses
  `uc->it_sources->add_at(4, is= ...)` to **insert** the six PCA sources at a chosen position
  so the default 8051 poll order is preserved (inserted in reverse: module #4 first).

Canonical shape:

```
uc->it_sources->add(is= new cl_it_src(uc, bmEXX,
                    sfr->get_cell(IE),  bmEXX,
                    sfr->get_cell(FLAG), bmFLAG,
                    0x0044, true, false, "spi", 6));
is->init();
```

### 4.3 Triggering from the peripheral
You never call into the interrupt system. You set the flag bit in the flag cell and the
poll loop finds it: `cl_it_src::pending()` reads `src_cell & src_mask`
(`src/core/sim.src/itsrc.cc:134+`), and acceptance clears it when `clr_bit` is set.
So the whole trigger is:

```
cell_flagreg->set(cell_flagreg->get() | bmFLAG);   // pca.cc:272, 285
```
Examples: `timer0.cc:245-250` (`cell_tcon->set(... | mask_TF)` then `overflow()`),
`pca.cc:246` (`| bmCF`), `pca.cc:272/285` (`| bmCCF[nr]`), `serial.cc:345-347`
(`scon_bits[0]->write(1)` — writing the *bit* cell so both views stay consistent),
`interrupt.cc:138-150` (`cl_interrupt::tick()` samples pin levels into `IE0`/`IE1`).

Priority: `cl_uc::priority_of()` is used by `cl_interrupt::print_info()`
(`src/sims/s51.src/interrupt.cc:190-200`); devices with a high-priority register override
`it_priority()` — `cl_uc89c51r::it_priority()` reads `IP` + `IPH`
(`src/sims/s51.src/uc89c51r.cc:227-243`, declared `uc89c51rcl.h:50`).

Slave sources (one vector, one parent) exist via `set_parent()` + `pass_over()`
(`src/core/sim.src/itsrccl.h:80-85`, `itsrc.cc:91-104`, driven from
`src/core/sim.src/uc.cc:3399-3412`); `search_it_src(cid_or_nr)` (`uccl.h:438`,
`uc.cc:3460-3472`) finds a source by `cid` if you need it.

---

## 5. Human-readable SFR names (traces, disassembly, `info var`, `dump sfr`)

Two gated tables in `src/sims/s51.src/uc51.cc`:

* `static struct name_entry sfr_tab51[]= { {cpu_mask, addr, "NAME"}, ... }` — starts
  `uc51.cc:67`, terminated `{0, 0, NULL}` at `uc51.cc:374`.
* `static struct name_entry bit_tab51[]` — starts `uc51.cc:381` (bit names, indexed in the
  `"bits"` space, e.g. `{CPU_89C51R|CPU_251, 0xdf, "CF"}` at `uc51.cc:468`).

Gating is a bit-AND against the device's `cpu_type`, applied once in
`cl_51core::make_vars()` (`src/sims/s51.src/uc51.cc:1320-1343`):

```
for (i= 0; sfr_tab51[i].name != NULL; i++)
  if (type->type & sfr_tab51[i].cpu_type)
    vars->add(sfr_tab51[i].name, sfr, sfr_tab51[i].addr, 7, 0, "");
```
so the mask field is a **bitmask of `CPU_*` flags, not an equality test** — a row shared by
several parts ORs the flags
(`{CPU_51R|CPU_89C51R|CPU_251, 0xa6, "WDTRST"}`, `uc51.cc:191`), and a row exclusive to the
RD2 carries a single flag (`{CPU_89C51R, 0x8f, "CKCON0"}` — `uc51.cc:198`, `0xaf "CKCON1"`
`:199`, `0xa2 "AUXR1"` `:200`). STC additions are grouped under
`/* STC12C5A60S2 */` at `uc51.cc:326-363` and STC15 delta rows at `:365-372` (they reuse
`CPU_STC12` — comment at `:365`). `bit_tbl()`/`dis_tbl()` hand the tables to the
disassembler (`uc51.cc:1367-1369`).

`CPU_*` flags: `src/core/utils.src/stypes.h:118-147` (`CPU_51R=0x10`, `CPU_89C51R=0x20`,
`CPU_251=0x40`, `CPU_STC12=0x4000`, aggregate `CPU_ALL_52` at `:141-145`).

Two other naming routes, for things the tables can't express:
* `cl_hw::register_cell(mem, addr, vname, vdesc)` — registers the cell *and* creates a
  described variable `src/core/sim.src/hw.cc:256-268`.
* `uc->vars->add(...)` directly, with the per-instance prefix idiom
  `chars pn= chars("", "%s%d_", id_string, id); uc->vars->add(pn+"base", cfg, cfg_base, ...)`
  (`src/core/sim.src/serial_hw.cc:229-237`, `src/core/motorola.src/cia.cc:83-95`,
  `src/core/motorola.src/pia.cc:84-92`).

**TRAP: one name, one binding — `cl_var_list::add()` calls `del(name)` before inserting**
(`src/core/sim.src/var.cc:361-366`), so adding a second address under an existing name silently
*removes* the first binding rather than failing or coexisting. A byte register and a bit-accessible
alias of the same latch therefore cannot both be called `P5`: the second registration wins and the
first disappears from `info var`/`dump sfr`. This is what forced `RD2_P5` (0xc7) and `RD2_P5_BIT`
(0xe8) apart; if an SFR name looks mysteriously absent, check for a later `add()` of the same
string before assuming the name table is wrong.

Fork-specific extra: `cl_uc_stc12` keeps a separate "this SFR address is defined on this part"
table used to report *unmodelled* SFR writes during trace
(`stc12_sfr_defined[]`/`init_sfr_defined()` at `src/sims/s51.src/ucstc12.cc:195-228`,
STC15 delta `init_sfr_defined_stc15()` at `:230-247`, consumption at `:798-808`).
**Note `0xCD SPSTAT`, `0xCE SPCTL`, `0xCF SPDAT` are already declared defined at
`ucstc12.cc:241-243` with no model behind them** — i.e. the STC15 SPI is a known hole.

---

## 6. Reset

Three distinct levels, all of which a peripheral author must understand:

1. **`cl_hw::reset()`** — per-element private state. Called for *every* hw by
   `cl_uc::reset()` (`src/core/sim.src/uc.cc:671-703`, loop at `:699-703`) and *also*
   standalone by the debugger command `reset [hw]`
   (`src/core/cmd.src/cmd_uc.cc:239-253`). Examples: `wdt.cc:91-96`, `pca.cc:321-328`,
   `serial.cc:349-364`, `interrupt.cc:151-154`, `uc390hw.cc:225-229`. Because `reset <hw>`
   bypasses the CPU, your `reset()` must be self-sufficient — do not assume SFRs got zeroed.
2. **`cl_51core::reset()`** (`src/sims/s51.src/uc51.cc:1736-1747`) = `cl_uc::reset()` then
   `clear_sfr()`. Device overrides chain up and then poke extra registers:
   `cl_uc51r::reset()` `src/sims/s51.src/uc51r.cc:105-112`;
   `cl_uc89c51r::reset()` `src/sims/s51.src/uc89c51r.cc:197-215` (re-sets `ECOM` in all five
   `CCAPMn` with `set()`, zeroes `IPH`, and fixes two RD2-specific values with a comment each).
3. **`clear_sfr()`** — *the per-SFR reset-value table for a device*. Base
   `cl_51core::clear_sfr()` zeroes `0x80..0xff` then writes the 8051 reset image with
   `sfr->write()` (`src/sims/s51.src/uc51.cc:1753-1789`: `P0..P3= 0xff`, `SP= 7`, etc.).
   Every device appends its own registers: `cl_uc52::clear_sfr()` `T2CON/TH2/TL2`
   (`uc52.cc:126-133`), `cl_uc51r::clear_sfr()` `SADDR/SADEN/AUXR` (`uc51r.cc:114-121`),
   `cl_uc89c51r::clear_sfr()` (`uc89c51r.cc:218-224`):

   ```
   cl_uc51r::clear_sfr();
   if (rd2_mem()) sfr->write(AUXR, 0x08);   // doc4235 Table 9-1: XRS=010 → 768B window
   if (rd2_mem()) sfr->write(CKCON0, 0);    // doc4235 Table 7-1
   for (int i= 0; i < 7; i++) x2_carry[i]= 0;
   ```

   Rules learned from this: values that must *not* re-trigger a peripheral hook use `set()`;
   values that must go through the model use `write()`; anything that depends on the exact
   part is gated by a predicate (`rd2_mem()` = `(type->type & CPU_89C51R) != 0`,
   `uc89c51r.cc:89-96`, because `cl_uc251` derives from this class — `uc251.cc:39-42`).
   `cl_uc_stc12::clear_sfr()` shows the per-part variant style with early return for the
   8052-shaped part (`ucstc12.cc:584-628`).

**Your peripheral's contribution to reset = `reset()` for its state + one or more
`sfr->write()` lines in the owning device's `clear_sfr()`** (and keep them consistent —
`clear_sfr` runs during `reset`, but `reset` also runs on the `reset` command alone).

---

## 7. Declaring a device (`-t 89C51R`)

* `struct cpu_entry { const char *type_str; enum cpu_type type; int subtype;
  const char *type_help; const char *sub_help; }` — `src/core/utils.src/stypes.h:309-316`.
* The table `struct cpu_entry cpus_51[]= { ... }` — `src/core/utils.src/globals.cc:344-415`,
  one row per spellable name, `{"89C51R", CPU_89C51R, CPU_CMOS, "89C51R", "cmos"}` at `:379`,
  STC rows `:403-412`, terminator `{NULL, CPU_NONE, 0, "", ""}` at `:414`.
  Declared `src/core/utils.src/globals.h:48-49`; bound to the app in
  `src/sims/s51.src/s51.cc:53` (`cpus= cpus_51;`).
* `-t NAME` resolution: `cl_sim51::mk_controller()`
  (`src/sims/s51.src/sim51.cc:66-119`) — `strcasecmp` scan over `type_str` (`:78-86`, default
  `"C52"` at `:79`), then `switch (cpus_51[i].type)` → `new cl_ucXXX(&cpus_51[i], this)`
  (`:88-117`; `case CPU_89C51R: return new cl_uc89c51r(...)` at `:96-97`).
  Unknown name → "Use -H option to see known types" (`:83-85`), so `-H` output is generated
  from this table.
* The device class takes the row in its ctor and keeps it as `type`
  (`cl_51core::cl_51core(struct cpu_entry *Itype, ...)` `src/sims/s51.src/uc51.cc:552-555`);
  `type->type` is what gates the name tables (§5) and any behaviour predicate
  (`rd2_mem()`), and `id_string()` re-derives the banner from the table
  (`uc51.cc:949-958`).
* **Sibling parts without a new class:** overload `subtype` with private bits and decode it.
  `{"STC15", CPU_STC12, CPU_CMOS|0x10, "STC STC15F2K60S2", "cmos"}`
  (`globals.cc:406`), `0x20` STC89, `0x30` STC15W (`:408-412`), decoded in
  `cl_uc_stc12::init()` → `stc_part` (`ucstc12.cc:328-347`) and then used for per-part
  `id_string()` (`:388-393`), hardware set (`:401-409`), SFR-defined set (`:340-347`) and
  reset values (`:584-628`). This is the cheapest way to add `AT89C51RD2` vs `AT89C51ED2`
  (the current model does the opposite: a separate `cl_uc251` subclass of `cl_uc89c51r` plus a
  `rd2_mem()` predicate).
* To add a *new* device you touch exactly: `globals.cc` row, `sim51.cc` `#include` + `case`,
  the new `ucXXX.cc/cl.h` (subclass of the nearest device — see
  `src/sims/s51.src/uc89c51rcl.h:38-65` for the smallest full shape), `objs.mk`,
  `sfr_tab51` rows, `clear_sfr()` values.
* To add a peripheral to an *existing* device you touch none of the above — §1 only.

---

## 8. Config variables (`cpu_aof_mdps`-style per-device addresses/masks)

Two separate mechanisms, both called "cfg".

### 8.1 Generic per-hw cfg space
`cl_hw` owns a hidden per-instance address space created in `cl_hw::init()`:
`<id_string>_<id>_cfg` + matching `cl_chip32`, every cell registered back to the hw
(`src/core/sim.src/hw.cc:89-105`), plus the `<id_string><id>_on` variable (`hw.cc:107-111`).
API: `cfg_size()` (`hwcl.h:89`, default 1), `cfg_cell()` `hw.cc:188-194`,
`cfg_set()` `hw.cc:196-200`, `cfg_write()` `hw.cc:202-206`, `cfg_get()`/`cfg_read()`
`hw.cc:208-218`, `cfg_help()` `hw.cc:220-226`, and the interception point
`conf()`/`conf_op()` (`hw.cc:160-186`) which runs on *every* cfg access because the default
`read()`/`write()` funnel into it (`hw.cc:145-158`).
Worked example of a peripheral with real cfg state: `src/core/motorola.src/piacl.h:36-56`
(enum of cfg slots) + `pia.cc:62-92` + `cfg_help` override, and
`src/core/sim.src/pc16550cl.h:66-67` / `serial_hw.cc:224-238`, `cia.cc:61-95`.
Variables become visible to `conf`/`set conf` because they are added to `uc->vars`.

### 8.2 The s51 CPU cfg slots (`uc51cpu_*`)
`enum uc51cpu_cfg { uc51cpu_aof_mdps= 1, uc51cpu_mask_mdps= 2, uc51cpu_aof_mdps1l= 3,
uc51cpu_aof_mdps1h= 4, uc51cpu_aof_mdpc= 5, uc51cpu_mask_mdpc= 6, uc51cpu_mdp_mode= 7 }` —
`src/sims/s51.src/uc51cl.h:272-279`. These live in the **cpu hw's** cfg space and are the
mechanism for "this part puts the bank-select bit at this address with this mask":

* Consumed by `cl_51core::decode_dptr()`: reads all six values
  (`src/sims/s51.src/uc51.cc:1224-1239`), picks SFR-banker vs chip-banker mode
  (`uc51.cc:1240-1303`, building `cl_banker(sfr, adps, mdps, dptr, 0, 0)` +
  `add_bank(1, memory("sfr_chip"), dpl1-0x80)`), and re-reads them at runtime to decide which
  bank is live (`uc51.cc:1536-1545`).
* Set by the device ctor/`init()` — `cl_uc521::init()` `cpu->cfg_set(uc51cpu_aof_mdps, 0x86)`
  … (`src/sims/s51.src/uc521.cc:42-46`); `cl_uc89c51r::init()` sets the *chip*-style pair
  `(uc51cpu_aof_mdpc= 0xA2, uc51cpu_mask_mdpc= 1)` then builds a 3-byte `dptr_chip` and calls
  `decode_dptr()` (`src/sims/s51.src/uc89c51r.cc:48-59`); `cl_uc_stc12::init()` sets the SFR
  quadruple + `uc51cpu_mdp_mode= 's'` and re-decodes
  (`src/sims/s51.src/ucstc12.cc:370-379`).

To generalise a peripheral across parts (e.g. an SPI block that moves address between parts)
follow 8.1: give it `<spi>_cfg { cfg_on=0, cfg_base=1, … }`, override `cfg_size()` and
`cfg_help()`, `cfg_set()` the defaults in the ctor, and publish names with `uc->vars->add()`.
To make a *device* property configurable, extend `uc51cpu_cfg` and read it where the behaviour
lives.

---

## 9. Tests

The fork has **two** harnesses.

### 9.1 Upstream in-tree baseline-diff harness
Layout (found by listing):
```
src/test-lib.mk               # shared: run-sim recipe, ELIDE, diff, %.c→%.ihx rules
src/test-target.mk            # per-sim: discovers test dirs, SIM path, baseline targets
src/app.mk:19-23              # "test:" → $(MAKE) -C test   /  "baseline:" → -C test baseline
src/sims/s51.src/test/Makefile         # includes src/test-target.mk
src/sims/s51.src/test/test-conf.mk     # CC = sdcc -mmcs51 ; AS = sdas8051 ; LD = sdld
src/sims/s51.src/test/{bander,banker,mdu,symdis,config}/Makefile + test-*.cmd + baseline/<name>
src/sims/s51.src/test/out/             # generated output dir
docs/testing.html                      # the harness documentation
```
Mechanics: `test-target.mk:14` discovers every `*/Makefile` as a test; `test-target.mk:41-46`
runs each in its own dir; `test-lib.mk` defines `all: _log_test out $(OUTPUTS)
_diff_to_baseline` (`test-lib.mk:59-77`) and diffs `out/` against `$(srcdir)/baseline`
(`test-lib.mk:84-90`, failures are swallowed with `|| true` — **the diff output is the verdict**);
`$(OUTPUTS): %: baseline/% Makefile test-lib.mk $(SIM)` (`test-lib.mk:78-82`) means an
**empty baseline file ⇒ test "passes" with empty output**, so baselines must be populated.
`run-sim` (`test-lib.mk:38-56`) invokes `$(SIM) -q -R 0 $(SIM_ARGS) <args>` with `%.cmd`
prerequisites turned into `-e 'exec "<file>"'`, `%.ihx` prerequisites passed as images, output
to `out/$@`, and `%.cmd`-less tests get `-g`. `ELIDE` (`test-lib.mk:29-36`) masks
version/copyright/timestamp lines. `%.ihx: %.c` is built with sdcc
(`test-lib.mk:104-113`), `%.ihx: %.rel` with sdas/sdld (`:115-121`).

Two real examples to copy:
* `test/banker/Makefile:1-7` — pure command-script test, two targets:
  ```
  OUTPUTS = C51 517
  $(OUTPUTS): %: test-%.cmd
  	$(call run-sim, -t $@)
  include test-lib.mk
  ```
  with the script `test/banker/test-C51.cmd` (comments + `info mem`, `memory cell sfr[0xd0]`,
  `var bank sfr[0xd0][4:3]`, `set mem …`, `info reg`) — this is the shape of a
  *register-model* test: prime registers, exercise, dump.
* `test/symdis/Makefile:1-13` — same pattern with `test-%.ihx test-%.cmd` and
  `DIFF_OPTS += -b` for whitespace-tolerant diffing; its `.ihx` files are checked in, which is
  how you avoid depending on an installed sdcc.
* `test/config/Makefile:1-6` — **the one that catches every peripheral addition**:
  ```
  OUTPUTS = $(filter-out Parameter, $(shell $(SIM) -H | sort -u -k2b | cut -d' ' -f1))
  $(OUTPUTS):
  	$(call run-sim, -t $(@F) -e 'conf; info reg; info mem')
  include test-lib.mk
  ```
  `conf` prints `Controller has N hardware element(s)` and one `on|off <id_string>[<id>]` line
  per hw (`src/core/cmd.src/cmd_conf.cc:69-74`; e.g.
  `test/config/baseline/89C51R:6` says 16 and lists `on wdt[0]`, `on pca[0]` at `:21-22`).
  Adding a peripheral **will** break `test/config` baselines for every affected `-t` type, and
  the `on|off` column is exactly where the un-initialised `on` of §1.3 would show up.

Exact invocations (documented at `docs/testing.html:26-79` and implemented at
`src/Makefile:82-93` + `src/app.mk:19-23` + `src/test-target.mk:26-46`):
```
cd ucsim && make test                          # every simulator
cd ucsim/src/sims/s51.src && make test         # just s51 (or: make baseline)
cd ucsim/src/sims/s51.src/test && make         # all s51 tests
cd ucsim/src/sims/s51.src/test && make config  # ONE test, verbosely
cd ucsim/src/sims/s51.src/test && make banker
# after an intended output change:
cd ucsim/src/sims/s51.src && make baseline     # or copy test/<x>/out/* → test/<x>/baseline/.
```
Writing a new peripheral test (`docs/testing.html:92-124`, verbatim recipe):
```
mkdir -p src/sims/s51.src/test/<name>/baseline
: > src/sims/s51.src/test/<name>/baseline/<TARGET>     # empty placeholder per output
cat > src/sims/s51.src/test/<name>/Makefile <<'EOF'
OUTPUTS = <TARGET>
<TARGET>: %: test-%.cmd
        $(call run-sim, -t $@)
include test-lib.mk
EOF
cd src/sims/s51.src/test && make <name>        # verify out/<TARGET> by eye
cp -r <name>/out/* <name>/baseline/.
```
The `SIM` under test is hard-wired to the just-built binary
(`test-target.mk:7-22`: `$(abs_top_builddir)/s51.src/ucsim_51`), and the harness refuses to run
if it is missing (`test-target.mk:30-33`).

### 9.2 The fork's own peripheral harness (this is what STC/BRT/ADC/WDT/T2 were tested with)
`../tests/` (i.e. `/home/oleksiy/s51/vendor/ucsim-stc/tests/`), run **from the fork repo
root**, per `../tests/README.md:1-30`:
```
./tests/smoke.sh                 # 11+ headless assertions against ucsim_51 -t STC12
./tests/smoke.sh path/to/ucsim_51  # skip the auto-build (smoke.sh:7, :13-16)
./tests/run_all.sh               # the whole rung_* matrix
./tests/diff_test.sh firmware.hex [cycles]   # differential vs emu8051-stc
./tests/trace.sh -fosc 11059200 -cycles 50000 x.hex > trace.tsv
```
A peripheral assertion is a piped command script — no framework, no sdcc, `set -e`, `pass()` /
`fail()` counters (`smoke.sh:8-11`) and a non-zero exit on failure. The three reusable shapes
(`../tests/smoke.sh`):
* registration: `$UCSIM -H | grep -q "STC12C5A60S2"` (`smoke.sh:22-23`);
* model present in `conf`: `printf 'conf\nquit\n' | $UCSIM -t STC12` (`smoke.sh:26-29`);
* **behaviour**: `printf 'set mem sfr 0x89 0x01\nset mem sfr 0x88 0x10\ntick 12\n'
  'dump sfr 0x8a 0x8a\nquit\n' | $UCSIM -t STC12` then parse the labelled `TL0:` line and
  compare (`smoke.sh:32-45`) — this is precisely the `tick [n]` command of §3.1 driving your
  `tick()` without executing code. ADC (`smoke.sh:52-57`, `:61-66`), PCA prescaler
  (`smoke.sh:68-72`), STC15 Timer2 rate (`smoke.sh:88-93`) and the STC12-vs-STC15 delta traps
  (`../tests/rung_stc15_sfr_delta.sh:1-8`) are all written this way.
Prebuilt firmware for the heavier rungs lives in `../tests/fixtures/*.ihx` (+ `.c` sources).

For a new peripheral: add one `rung_<periph>.sh` to `../tests/` (register + `conf` presence +
rate/flag assertions via `set mem`/`tick`/`dump sfr`), **and** update the in-tree
`test/config` baselines from §9.1, since the hw list changes.

---

## 10. Documentation

* `docs/testing.html` — the test harness contract; the only in-tree doc that is genuinely
  maintained for developers (`docs/testing.html:26-124`). `docs/cpu_types.html` documents the
  `-t` types (89C51R section at `docs/cpu_types.html:104-125`, and it admits "Nothing
  implemented yet" for the 251 delta at `:125`); `docs/invoke.html:288` lists the accepted
  `-t` values. **Neither was updated for the STC12/STC15 work** — `grep STC docs/` finds
  nothing — so in-tree CPU docs are stale and updating them is *not* the fork's convention.
* `NEWS` lists new simulators and new device types per release
  (`NEWS:1-9`: "Intel 8048 simulator", "New device type in z80 simulator: r800") — upstream
  convention, but the fork did **not** add STC entries there.
* The fork's actual convention is *spec-first, evidence-after, in the wrapper repo*:
  `../README.md:120-124` — "Peripheral behaviour follows STC12-PERIPHERAL-MODEL.md …
  Resolutions must be recorded in the spec first, then adopted by both implementations";
  behavioural evidence tables in `../RESULTS.md` (e.g. `:274-286` for the STC15 delta,
  `:444-448` for ADC); proposed spec changes as numbered documents in `../spec-updates/`
  (`001-adc-start-clear-timing.md`, `010-stc89-peripheral-model.md`, …); test documentation in
  `../tests/README.md`; discovered contradictions tracked in `../CLOSE-OUT.md:58-60`
  (e.g. "PERIPHERAL-MODEL 'IE.EC enables PCA' — no EC exists").
* Per-file: every fork peripheral carries a header comment naming the registers, the bit
  meanings, and the spec section it implements —
  `stc15_timer2.cc:13` ("Per STC15-PERIPHERAL-MODEL.md §2.2 and §3"),
  `stc12_adc.cc:109-110` (`§4`), `pca.cc:5` and `pca.cc:309-312` (`§5.3`, plus an explicit
  "9th bit not yet modelled" note), `stc12_adc.cc:14-18` (what is synthetic and why),
  and the RD2 work cites the datasheet directly (`regs51.h:247-249` "doc4235 table 7-1",
  `uc89c51r.cc:100-111`, `uc89c51r.cc:208-213`). Match that: cite the datasheet table in the
  comment, state the unmodelled corners, and note which part-specific traps were verified.

---

## 11. Numbered recipe: add an SPI port

Two live targets: the **AT89C51RD2 SPI** (`CKCON1` at `0xaf` is already defined as
"SPI X2 select" in `src/sims/s51.src/regs51.h:70` but no SPI model exists) and the
**STC15 SPI** (`SPSTAT 0xCD`/`SPCTL 0xCE`/`SPDAT 0xCF` are already declared *defined* at
`src/sims/s51.src/ucstc12.cc:241-243` with nothing behind them). Steps below are for
`cl_spi_89c51r`; the STC15 variant differs only in the owning `mk_hw_elements` and addresses.

1. **Declare the registers.** In `src/sims/s51.src/regs51.h`, next to the RD2 block at
   `:67-70` and `:241-257`, add `#define SPSTAT/SPCTL/SPDAT` addresses and `bm…` masks for the
   control/status bits (`bmSPIF`, `bmWCOL`, …), with the datasheet table number in the comment
   — style to copy: `regs51.h:247-257`.
2. **Declare the class** in `src/sims/s51.src/spi_89c51rcl.h`, shape =
   `src/sims/s51.src/stc12_brtcl.h:17-29`: `class cl_spi_89c51r: public cl_hw` with
   `class cl_memory_cell *cell_spstat, *cell_spctl, *cell_spdat;` plus your state (bit counter,
   shift register, `int prescaler;`) and
   `cl_spi_89c51r(class cl_uc *auc); virtual int init(void); virtual void
   added_to_uc(void); virtual void write(class cl_memory_cell *cell, t_mem *val);
   virtual int tick(int cycles); virtual void reset(void); virtual void
   print_info(class cl_console_base *con);`. Include `"hwcl.h"` as `stc12_brtcl.h:15` does.
3. **Construct.** `cl_hw(auc, HW_TIMER /* or HW_UART */, 13, "spi")` — pick a free id (used:
   0,1,2,3,10,11,12 — see §1.1) and initialise every member in the body, as
   `stc12_brt.cc:27-34` / `stc12_adc.cc:31-40` do.
4. **`init()`: chain up first.** `cl_hw::init();` — exactly like `stc12_adc.cc:43`,
   **not** like `stc12_brt.cc:37-50` (§1.3). Then
   `class cl_address_space *sfr= uc->address_space(MEM_SFR_ID);` and
   `cell_spctl= register_cell(sfr, SPCTL); cell_spstat= register_cell(sfr, SPSTAT);`
   (both need callbacks — a write to `SPSTAT` from software clears `SPIF`/`WCOL` by writing 1,
   so you must intercept it) and `cell_spdat= sfr->get_cell(SPDAT);` (data register, written
   by the model). If `SPSTAT` is bit-addressable on your part, also
   `uc->address_space("bits")` + `register_cell(bas, SPSTAT+n)` and reassemble in `write()`
   the way `cl_timer0::write()` does (`src/sims/s51.src/timer0.cc:110-117`, `:150-165`);
   `pca.cc:80-81` is the one-liner version.
   If the SPI feeds or is fed by another element, `make_partner(HW_UART, 0)` etc. here
   (`stc12_brt.cc:46-47`).
5. **`write()`.** Dispatch on `cell ==` your cells (`src/sims/s51.src/stc12_adc.cc:62-86`);
   decode the enable/speed bits into members; for auto-clearing bits edit `*val`
   (`src/sims/s51.src/stc12_wdt.cc:39-52`) or `cell->set()` (`stc12_adc.cc:128`). Keep `read()`
   unoverridden so `conf()`/`<name>_on` keep working (§2.2).
6. **`tick()`.** Return `resGO`. Take your clock through the CPU so CKCON1's SPI-X2 bit works:
   `cycles= ((cl_51core *)uc)->peripheral_cycles(cycles, bmSPX2);` — the call shape is
   `src/sims/s51.src/timer0.cc:211-213` / `wdt.cc:79` / `pca.cc:215`; add the new bit to the
   `bmX2…bmWDX2` family in `src/sims/s51.src/regs51.h:251-257` and make sure it has a slot in
   the carry array (`src/sims/s51.src/uc89c51rcl.h:62-64`, `uc89c51r.cc:122-141` — it is sized
   `7`, one slot per CKCON0 bit, so a CKCON1 bit needs its own slot or the array must grow).
   Then count with a remainder: modulo prescaler (`stc12_brt.cc:74-79`) or countdown
   (`stc12_adc.cc:90-96`). Remember `cycles` is always 1 (§3.1). Gate on
   `uc->state == stIDLE` if the part halts the block in idle (`pca.cc:207-209`).
   On completion, push the received byte with `cell_spdat->set(...)` and raise the flag with
   `cell_spstat->set(cell_spstat->get() | bmSPIF)` — that is all it takes for the interrupt to
   fire (§4.3). To notify a partner instead: `inform_partners(EV_OVERFLOW, 0)`
   (`src/sims/s51.src/stc12_brt.cc:100`).
7. **`added_to_uc()`: the interrupt source.** Copy `src/sims/s51.src/timer0.cc:120-142`:
   ```
   class cl_address_space *sfr= uc->address_space(MEM_SFR_ID);
   class cl_it_src *is;
   uc->it_sources->add(is= new cl_it_src(uc, bmESPI, sfr->get_cell(IE), bmESPI,
                       sfr->get_cell(SPSTAT), bmSPIF,
                       <vector>, true, false, "spi", <poll_priority>));
   is->init();
   ```
   Argument meanings: `itsrc.cc:47-80` / `itsrccl.h:55-82`. If your part lacks a global enable
   bit, use the per-module-cell trick from `src/sims/s51.src/interrupt.cc:85-119`; if several
   sources share a vector and must not perturb the default poll order, insert with
   `uc->it_sources->add_at(n, …)` as `src/sims/s51.src/pca.cc:90-126` does. If the part has a
   priority-high register, make sure `it_priority()` covers your new IE bit
   (`src/sims/s51.src/uc89c51r.cc:227-243`).
8. **`reset()`.** Zero bit counters and remainders — `src/sims/s51.src/pca.cc:321-328`,
   `wdt.cc:91-96`, `serial.cc:349-364`. It must stand alone: `reset <hw>` calls only this
   (`src/core/cmd.src/cmd_uc.cc:239-253`).
9. **`print_info()`.** One or two `con->dd_printf()` lines — `stc12_brt.cc:106-121`,
   `stc12_wdt.cc:82-90`, `wdt.cc:98-102`. This is what `info hardware <name>` prints
   (`src/core/cmd.src/cmd_info.cc:146-158`, which also calls `print_cfg_info()`), and the
   `-u spi` option attaches a console to it (`src/core/utils.src/app.cc:393`).
10. **Register it on the device.** In `cl_uc89c51r::mk_hw_elements()`
    (`src/sims/s51.src/uc89c51r.cc:63-73`) append
    `add_hw(h= new cl_spi_89c51r(this)); h->init();`. For a part-conditional block use the
    `has_*` flag style of `src/sims/s51.src/ucstc12.cc:401-409` + `:530-547`, and gate on a
    predicate like `rd2_mem()` (`uc89c51r.cc:89-96`) when a sibling class inherits yours
    (`uc251.cc:39-42`).
11. **Name the registers.** Add `{CPU_89C51R, 0xxx, "SPSTAT"}` style rows to `sfr_tab51` in
    `src/sims/s51.src/uc51.cc` — put them in the RD2 group at `:198-200`, and add bit names to
    `bit_tab51` (`uc51.cc:381+`, CCON group at `:467-474` is the model). Keep the table
    terminators last (`uc51.cc:374`). Gating rule and consumer: `uc51.cc:1330-1342`. For the
    STC15 variant add `{CPU_STC12, 0xcd, "SPSTAT"}` … rows in the STC15 group at
    `uc51.cc:365-372` (they are already *declared defined* at `ucstc12.cc:241-243`, so nothing
    there needs changing).
12. **Reset values.** Add the SPI SFR reset image to `cl_uc89c51r::clear_sfr()`
    (`src/sims/s51.src/uc89c51r.cc:218-224` — one `sfr->write(reg, val)` per register with the
    datasheet table cited), and, if a value must also be forced on a bare `reset`, in
    `cl_uc89c51r::reset()` (`uc89c51r.cc:197-215`). Use `set()` where a write would re-enter
    your `write()` hook with the wrong semantics (`uc89c51r.cc:201-205`) and `write()` where
    the hook must see it (`uc89c51r.cc:222`).
13. **Build it.** Add `spi_89c51r.o` to `OBJECTS` in `src/sims/s51.src/objs.mk:5-12`, then
    `cd ucsim && make` (top `Makefile:2-3` → `src/Makefile:20-23` → `src/app.mk:1-2`,
    link rule `src/app.mk:25-32`). `make dep` regenerates `Makefile.dep`
    (`src/app.mk:82-91`). Optional separate-binary pattern: `src/sims/s51.src/Makefile:35-38`.
14. **Test it.**
    * in-tree: `cd ucsim/src/sims/s51.src/test && make config` → expect a diff (hw count and a
      new `on spi[13]` line, `src/core/cmd.src/cmd_conf.cc:69-74`); inspect `out/*`, then
      `cd .. && make baseline`. Add a `test/<name>/` dir per §9.1 for a permanent register
      test.
    * fork harness: add `../tests/rung_spi.sh` modelled on `../tests/smoke.sh:32-45` —
      `printf 'set mem sfr 0xce <SPCTL>\nset mem sfr 0xcf 0x55\ntick N\ndump sfr 0xcd 0xcf\nquit\n'
      | ucsim/src/sims/s51.src/ucsim_51 -t 89C51R`, assert `SPIF` set after exactly N clocks
      and `SPDAT` holds the exchanged byte; run `./tests/smoke.sh` and `./tests/run_all.sh`
      from `../` to prove no regression.
15. **Document it.** Header comment with register map, bit meanings, datasheet table, and the
    unmodelled corners (style: `src/sims/s51.src/stc12_adc.cc:1-20`,
    `stc15_timer2.cc:1-16`, `uc89c51r.cc:100-111`). Record behaviour/evidence in the wrapper
    repo the way the fork does — `../README.md:120-124` (spec first), `../RESULTS.md` tables,
    `../spec-updates/NNN-*.md` for proposed resolutions, `../tests/README.md` for the new
    script. Do **not** expect `ucsim/docs/` or `NEWS` to be updated; they were not for the STC
    work (§10).

### Behavioural corner-cases this tree has already been burned by
* A `tick()` that assumes `cycles > 1` happens. `tick_hw` guarantees 1
  (`src/core/sim.src/uc.cc:2993-3003`).
* Counting in "ticks" and claiming "clocks": `stc12_adc.cc:70-73`.
* Reusing an `id` another peripheral switches on: `serial.cc:370-392`.
* Forgetting `cl_hw::init()`: `stc12_brt.cc:37-50`.
* A mode/enable bit change that leaves stale edges or carries behind — reset them in the
  `write()` handler, not only in `reset()`: `pca.cc:136-140` (`clk_source` change zeroes
  `t0_overflows`/`ECI_edge`), `pca.cc:163-167` (`ccapm` change clears edge flags),
  `timer0.cc:170-186` (`T_edge= 0`).
* A half-cycle carry that survives a mode switch: `uc89c51r.cc:129-133` zeroes `x2_carry`
  whenever the half-rate condition is off.
* Peripherals that should freeze in idle: `pca.cc:207-209`, `stc12_wdt.cc:59-61`.
* Registers that exist on one sibling part and not another: gate with a predicate and refuse
  the write rather than silently modelling it
  (`src/sims/s51.src/ucstc12.cc:408` "P5 is STC15 only — refused on STC12", `:598-627`,
  and `rd2_mem()` for the RD2/251 split).
