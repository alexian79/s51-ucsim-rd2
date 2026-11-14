# s51: a port latch write emits two EV_PORT_CHANGED events, so every counter input counts each edge twice

## Symptom

In counter mode, one external edge on T2 (P1.0) advances `TH2:TL2` by **two**. Measured with
`s51.src/t2cnttest.py` (in `evidence/`, 30 legs): 21 of its 30 legs are wrong before the change
and 30/30 after.

| stimulus | expected | before | after |
|---|---|---|---|
| falling edge via a latch write (`MOV P1,#…` clearing P1.0) | +1 | **+2** | +1 |
| falling edge via external pin drive | +1 | +1 | +1 |
| rising edge only, static level, T2EX (P1.1) edge | +0 | +0 | +0 |

It is not device-specific and not timer-specific: identical on `-t 89C51RD2`, `-t C52` and on a
build predating all of my other work. Both timer-2 counter modes are affected (capture and
auto-reload), and so are **timer 0, timer 1 and the PCA external clock input**, all from the one
cause below.

## Root cause

`cl_port::write()` updates the read-only `port_value` cfg mirror with a **device-visible** write,
and the port is subscribed to its own cfg cells:

- `cl_hw::init()` (`core/sim.src/hw.cc:104`) runs `cfg->register_hw(a, this, false)` for every cfg
  cell, so the port element is a memory operator on its own mirror cell.
- `cfg->write(port_value, …)` therefore re-enters `cl_port::write()`. The inner pass takes neither
  mirror branch (its `cell` is the cfg cell), passes through `conf()` / `conf_op()`, and still
  reaches the event block — `inform_partners(EV_PORT_CHANGED, &ep)`.

So a single latch write produces two events, and `T_edge` reaches 2 per edge. `do_t2_capture()`
and `do_t2_reload()` consuming `cycles = T_edge` are correct; the duplicate is generated upstream
of them. Verified live with an instrumented build (`T2DBG=1`):
`depth=0 → depth=1 → conf_op addr=2 → inform ×2 → [cap] cycles->2`.

Pin-driven edges (`cl_port::set_pin()`) were never doubled: that path informs **before** touching
the mirror, and in the re-entrant pass `ep.pins == ep.new_pins`, so the emit guard fails. That
asymmetry is why the defect hid for so long — driving the pin from the console looks correct, and
writing the latch, which is what a test program does, does not.

## Fix

`src/sims/s51.src/port.cc`, three sites — update the mirror without notifying. This is exactly what
`cl_port::init()` already does at `port.cc:105`, and `conf_op()`'s `port_value` case stores the
same value through `cell->set()` either way, so the mirror's contents are unchanged.

```
-	cfg->write(port_value, port_pins & nv);
+	cfg->set(port_value, port_pins & nv);
```

`cl_memory_cell::set()` (`core/sim.src/mem.cc:1262`) differs from `write()` only in skipping the
operator chain; both honour `CELL_READ_ONLY`, which in this tree is set only on ROM ranges in the
`f8` and `stm8` models, never on a cfg cell.

A timer-side dedupe was rejected: both events carry identical payloads, so a subscriber cannot
distinguish "the same edge twice" from two genuine edges. The fix belongs at the emit site.

## Blast radius

`EV_PORT_CHANGED` has four subscribers in `s51.src`: `timer0.cc:398` (T0/T1 — doubled, measured),
`timer2.cc:399` (doubled, measured), `pca.cc:368` (ECI doubled, measured; its captures and flags
are idempotent) and `interrupt.cc:170` (idempotent). `rd2_kbd`/`rd2_spi`/`rd2_brg` do not subscribe.
`cl_port` is shared by every model in this family, so the change also affects the STC devices —
their counter inputs were doubled in exactly the same way.

## Testing

- `evidence/t2cnttest.py`: 30 legs, one count per edge for T0, T1, T2 (counter and capture) and PCA
  ECI, driven both by latch write and by pin drive, with negative legs (rising-only, static level,
  `TR2=0`, `T2EX≠T2`, `C/T2=0` differenced pair). **9/30 → 30/30**; the 9 legs that pass beforehand
  are the negative and invariant ones, listed in the suite.
- Full simulation floor of my project, 22 suites, green with the change in.
- `evidence/results.txt` has both runs; `evidence/subscribers.txt` the handler list.
- The patch is generated against pristine `59a14ff9` (v0.9.12) and applies with `patch -p1` from
  the repo root; the result is byte-identical to the source that produced the tested build. The
  verification build is a full ucsim build of the same `port.cc` (these three sites are
  byte-identical in v0.9.12) from the CrispStrobe fork; I did not rebuild upstream itself.

## Relation to the timer-2 `cycles` patch

Independent and order-free. That one makes `do_t2_reload()`/`do_t2_capture()` consume their
`cycles` argument; this one stops `cycles` (= `T_edge`) from arriving as 2 for one edge. Both are
needed for counter mode to be right: with only the first, each edge still counts twice; with only
the second, `T_edge` is correct but still discarded.
