# s51: timer 2 reload and capture must honour their `cycles' argument

Patch: `timer2-cycles.patch` (1 file, +12/-10, `src/sims/s51.src/timer2.cc`)
Base: `master` @ `59a14ff9` ("Merge branch 'dev', up to v0.9.12"), verified to
apply with `git apply` onto a pristine checkout of that commit.

## Problem

`cl_timer2::tick(int cycles)` dispatches the elapsed-cycle count to the mode
handler. Three of the five handlers loop over it; two ignore it:

```
src/sims/s51.src/timer2.cc:176  cl_timer2::do_t2_baud(int cycles)      ... while (cycles--)  (:192)
src/sims/s51.src/timer2.cc:211  cl_timer2::do_t2_capture(int cycles)   <-- never reads cycles
src/sims/s51.src/timer2.cc:240  cl_timer2::do_t2_reload(int cycles)    <-- never reads cycles
src/sims/s51.src/timer2.cc:268  cl_timer2::do_t2_down(int cycles)      ... while (cycles--)  (:281, :296)
src/sims/s51.src/timer2.cc:323  cl_timer2::do_t2_clock_out(int cycles) ... while (cycles--)  (:339)
```

As of `59a14ff9` the two offenders are, verbatim (`:227` and `:256`):

```c
227:  if (!cell_tl->set(cell_tl->get() + 1))
228:    {
229:      if (!cell_th->set(cell_th->get() + 1))
230:	cell_tcon->set(cell_tcon->get() | bmTF2);
231:    }
```
```c
256:  if (!cell_tl->set(cell_tl->get() + 1))
257:    {
258:      if (!cell_th->set(cell_th->get() + 1))
259:	{
260:	  cell_tcon->set(cell_tcon->get() | mask_TF);
261:	  cell_th->set(cell_rcap2h->get());
262:	  cell_tl->set(cell_rcap2l->get());
263:	}
264:    }
```

Both functions *do* compute a real cycle count immediately above:

```c
253:  if (C_T)
254:    (cycles= T_edge), T_edge= 0;
```

`T_edge` is the number of pending falling edges on the T2 pin, accumulated by
`cl_timer2::happen()` (`timer2.cc:362`, `T_edge++` at `:374`). It is a count,
and both functions throw it away: they add exactly **one** count per `tick()`
no matter how many events are waiting, and — because the increment is not
guarded by `cycles` at all — they add one count even when **zero** events
arrived. In external-counter mode a timer 2 in *both* reload and capture mode
therefore ignores its input pin and counts simulator ticks instead.

This is not a device-model bug. `timer2.cc` is the shared mcs52 timer 2 used by
every `CPU_ALL_52` part; the repro below runs on `-t C52`, a plain 8052, with
no vendor extension involved.

## Where it is invisible, and why

In timer mode (`C/T=0`) the patch changes nothing, because the only caller of
`cl_hw::tick()` in the whole tree is `cl_uc::tick_hw()`:

```c
src/core/sim.src/uc.cc:2939:  while (c-- > 0)
src/core/sim.src/uc.cc:2940:    {
src/core/sim.src/uc.cc:2942:      for (i= 0; i < hc; i++)
src/core/sim.src/uc.cc:2945:          hw= (class cl_hw *)(hws->at(i));
src/core/sim.src/uc.cc:2947:	      hw->tick(1);
```

`post_inst()` calls `tick_hw(inst_ticks)` (`uc.cc:3283`) and that loop fans the
instruction's machine cycles out as individual `tick(1)` calls, so `cycles` is
always 1 here and looping over it is a no-op. (Grepping `->tick(` over `src/`
returns exactly this one call site.) It follows that:

* nothing in timer mode moves — see the control below; and
* `cycles` is the *only* channel by which a timer learns how long it has been
  run. A handler that never reads it cannot ever honour a per-peripheral clock
  scale (an `X2`-style double-rate mode, a peripheral prescaler, a slower
  peripheral clock domain): it advances on ticks it must skip and stands still
  on spans it must cover. `do_t2_baud()` and `do_t2_clock_out()` already rely
  on this argument, scaling it by 6 to get their Fosc/2 rate; the two offenders
  are the only ones that do not.

So the report stands on two legs: a **demonstrable data-loss bug in counter
mode today** (below), and a **contract violation that blocks any clock-scale
work** in timer 2.

## Minimal repro

Three commands, no external activity at all. Timer 2 in auto-reload,
`C/T=1` (counter mode), started, then 5 ticks with the T2 pin untouched:

```
$ ucsim_51 -q -t C52
set memory sfr 0xc8 0x06        # T2CON = C_T2 | TR2
tick 5
dump sfr 0xcc 0xcc              # TL2
```

| binary | TL2 |
|---|---|
| `master` @ `59a14ff9` | `0x05` — counted 5 events that never happened |
| patched | `0x00` — correct |

Real pulses are ignored just as reliably: queue 1..6 falling edges on P1.0
(T2) before a single `tick 1`, and the unfixed build counts 1 in every case:

| pending edges | unfixed TL2 | patched TL2 |
|---|---|---|
| 1 | 1 | 2 |
| 2 | 1 | 4 |
| 3 | 1 | 6 |
| 4 | 1 | 8 |
| 6 | 1 | 12 |

Capture mode (`T2CON=0x07`) behaves identically — same table, same numbers —
because `do_t2_capture()` has the same defect.

The patched column is `2 x edges` rather than `1 x edges`: a latch write on
this device produces **two** `EV_PORT_CHANGED` notifications
(`port.cc:191` in `cl_port::event()` and `port.cc:247` in `cl_port::set_pins()`
both fire for one `set memory sfr`), so each simulated pulse is queued twice.
That doubling is present in both binaries, is unrelated to this patch, and
does not affect the comparison — the point is that the unfixed count is flat at
1 while the fixed count tracks the pending events. It is worth a separate
look, and it is also the reason a counter-mode timer built on top of the port
write path currently over-counts.

## Control: nothing else moves

`evidence/config_baseline_diff.sh` runs the s51 config test's own command
(`-q -R 0 -t <dev> -e 'conf; info reg; info mem'`, with the ELIDE filter copied
verbatim from `src/test-lib.mk`) for all 14 advertised devices against both
binaries:

```
devices compared: 14
RESULT: fixed output is byte-identical to unfixed for every device
```

No baseline row moves; nothing was regenerated.

`evidence/timer2_cycles_demo.py` case 4 additionally pins timer mode across
three instruction mixes that burn the *same* machine cycles — 105 `MOV`,
55 `MUL AB`, or 205 `NOP` (2520 clks / 210 machine cycles each):

| mix | instructions | clks | unfixed TL2 | patched TL2 |
|---|---|---|---|---|
| MOV | 105 | 2520 | 202 | 202 |
| MUL | 55 | 2520 | 202 | 202 |
| NOP | 205 | 2520 | 202 | 202 |

Identical across mixes and across binaries — the timer-mode count is already
carried by `tick_hw`'s per-machine-cycle fan-out, exactly as the analysis above
predicts.

## Build and evidence

Host: Linux x86-64, g++ / GNU make / bison / flex present.

```
./configure            # exit 0; "checking for curses.h... no", no packages installed
make -j32              # exit 0; all simulator binaries incl. src/sims/s51.src/ucsim_51
```

Reproduce everything:

```
evidence/run-all.sh <unfixed-ucsim_51> <patched-ucsim_51>
```

It regenerates `evidence/transcripts/` (raw console transcripts per case, one
per binary) and the `evidence/out-config.*` config dumps, and re-asserts the
case table. The transcripts show the piped-console echo interleaving described
in issue #14; the scripts read the last dump, which is the authoritative one.

## Two harness notes found while getting the project's own test to run

They are not touched by this patch, but they mean `make test` cannot gate
anything at HEAD:

1. `src/Makefile:95` builds `PKGS_TEST = $(foreach pkg, sim.src $(PKGS), $(pkg)/test)`
   from the bare package names, while `PKGS` are compiled with `-C sims/$@`
   (`src/Makefile:61`). `$(PKGS_TEST)` then does `-C $(dir $@) test`, i.e.
   `-C s51.src/`, which does not exist. Every entry fails and is swallowed:
   `make -C src test` prints `No rule to make target 'test'` / `s51.src/: No
   such file or directory` and exits 0.
2. `src/test-lib.mk:85` states the prerequisite as `$(srcdir)/../../../test-lib.mk`;
   from `src/sims/s51.src/test/config` that resolves to `src/sims/test-lib.mk`
   (one level short of `src/test-lib.mk`), so the test stops with
   `No rule to make target`.

Also note the checked-in `src/sims/s51.src/test/config/baseline/*` files no
longer match HEAD's own output (14/14 files differ, cosmetic only: the
copyright-year elision form, a `?` in the `info reg` disassembly column, bank
list indentation). That drift is present with the unfixed binary too, so it
predates this patch; it is reported here so nobody reads it as fallout.

## Not previously reported

Searched the tree and the tracker at `59a14ff9`:

* `git log --all -- src/sims/s51.src/timer2.cc` → only `98639080 initial` era
  content plus `66cda0e9 reorg`, `c1db5589 update comments`, `6e4d431d remove
  old consts`. `do_t2_reload` last changed in `1082ad11 reorg memory creation`.
* `master`, `dev` and `reorg` carry byte-identical `do_t2_reload` /
  `do_t2_capture` bodies — unfixed on all three.
* GitHub: 19 issues + 1 pull request in `danieldrotos/ucsim`, none about timer 2,
  `do_t2_reload`, `tick_hw` or the `cycles` contract (issue #15 is the nearest
  neighbour: pin-level events on the 8051 ports).
