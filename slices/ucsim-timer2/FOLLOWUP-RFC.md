# RFC / inventory: upstreaming the AT89C51RD2/ED2 support from the ucsim-stc fork

Scope: **inventory and sequencing only** — nothing here is implemented, and no
file outside `/home/oleksiy/s51/upstream/ucsim-timer2/` was touched to produce
it. All numbers are measured on the live fork at
`/home/oleksiy/s51/vendor/ucsim-stc` (branch `master`, `441710c`), not quoted
from a plan.

## 1. The fork is not mainline-based

```
origin  https://github.com/CrispStrobe/ucsim-stc.git
git diff --stat  ->  23 files changed, 1418 insertions(+), 172 deletions(-)
```

The fork's parent already carries an STC12/STC15 device family that mainline
`danieldrotos/ucsim` does **not** have (`grep -c CPU_STC12` in
`/tmp/ucsim-mainline/src/core/utils.src/stypes.h` → `0`; `objs.mk` in the fork
lists `stc12_brt.o stc15_timer2.o`, files mainline has never had). Consequences
that shape every step below:

* This is a **re-port, not a cherry-pick**: no commit in the fork can be
  `git am`-ed onto mainline.
* The `--stat` **overstates** the transferable work: hunks that exist only to
  fix STC interactions are dead weight upstream. Concrete example: the whole
  `interrupt.cc` hunk (`cl_interrupt::added_to_uc()`, 9/1) is a guard
  `if (!(uc->type->type & CPU_STC12)) return;` that stops STC PCA vectors from
  squatting on `003Bh` — upstream has no STC vectors, so the hunk has no
  subject.
* The `--stat` also **understates** it: mainline reorganised the tree
  (`66cda0e9 reorg: move srcdirs to new place`), so paths and build files must
  be re-derived, and peripheral files the RD2 reuses (`timer2.cc`, `pca.cc`,
  `serial.cc`, `uc51.cc`) have diverged since the fork's base.

## 2. Triage of the tracked diff

| file | +/− | class | why |
|---|---|---|---|
| `docs/Makefile` | 1/1 | **DROP** | local `prefix = /home/oleksiy/s51/install` |
| `src/common.mk` | 5/5 | **DROP** | local build: `LEX = :`, `YACC = yacc`, `CURSES_LIBS =` |
| `src/packages.mk` | 1/1 | **DROP** | local `prefix` |
| `core/utils.src/ddconfig.h` | 3/3 | **DROP** | generated file committed with curses/flex disabled by hand |
| `s51.src/objs.mk` | 1/1 | atomic unit | adds `rd2_brg.o rd2_kbd.o rd2_spi.o rd2_boot.o` |
| `s51.src/Makefile` | 5/3 | atomic unit | build rules for the four new objects |
| `s51.src/interrupt.cc` | 9/1 | **DROP upstream** | STC-only guard (see §1) |
| `core/utils.src/stypes.h` | 19/1 | atomic unit | `CPU_89C51RD2 = 0x8000` + mandatory insertion into `CPU_ALL_52` |
| `core/utils.src/globals.cc` | 5/0 | atomic unit | `-H` / `-t` rows `89C51RD2`, `89C51ED2` |
| `s51.src/sim51.cc` | 6/1 | atomic unit | `case CPU_89C51RD2: new cl_uc89c51rd2(...)`; un-comments `uc89c51rcl.h` |
| `s51.src/uc89c51r.cc` | 413/5 | atomic unit | the RD2/ED2 model itself (`cl_uc89c51rd2`) |
| `s51.src/uc89c51rcl.h` | 74/1 | atomic unit | class declaration for the above |
| `s51.src/uc51.cc` | 199/63 | **splits** | `sfr_tab51` / `bit_tab51` rows (device table, ships with the device) vs `do_interrupt()` + `priority_of()` changes (shared 51 core — needs its own proof) |
| `s51.src/regs51.h` | 104/0 | ships separately | pure addition of SFR addresses and bit masks; harmless alone, but meaningless until a consumer lands |
| `s51.src/timer2.cc` / `timer2cl.h` | 44/10, 5/0 | **splits** | the `do_t2_reload`/`do_t2_capture` `cycles` loop (already upstreamed by `timer2-cycles.patch`) vs RD2 `T2OE`/CKRL plumbing (device) |
| `s51.src/pca.cc` / `pcacl.h` | 83/36, 1/0 | **general fixes** | `reset()`, `tick()`, `do_pca_counter()`, `do_pca_module()`, `happen()` — affects every part with a PCA; upstream-relevant on its own, must not ride along with a device PR |
| `s51.src/wdt.cc` | 16/1 | **general fix** | `tick()` + `write()` (WDTPRG period) — affects existing watchdog users |
| `s51.src/timer0.cc` | 6/1 | **general fix** | `cl_timer0::tick()` — affects every 51 |
| `s51.src/serial.cc` / `serialcl.h` | 374/38, 37/0 | **highest risk** | touches `cl_serial` ctor, `init`, `new_hw_added`, `write`, `conf_op`, `serial_bit_cnt`, `tick`, `reset`, `happen`, `print_info` — the shared UART of *every* 51 device |

Untracked new peripherals (all new files, so mechanically easy; the hard part
is the register/interface contract each implies):

| files | lines | peripheral |
|---|---|---|
| `rd2_spi.cc` / `rd2_spiccl.h` | 816 + 123 | SPI master/slave |
| `rd2_boot.cc` / `rd2_bootcl.h` | 426 + 133 | boot-ROM / `ENBOOT` vector remap |
| `rd2_kbd.cc` / `rd2_kbdcl.h` | 259 + 111 | keyboard interrupt port |
| `rd2_brg.cc` / `rd2_brgcl.h` | 147 + 51 | fixed-rate baud generator (BRG) |

Tracked + untracked ≈ **3.5 k lines**, of which roughly 2.5 k is device-only
(`uc89c51r.cc`/`uc89c51rcl.h` 487 + the four new peripherals 2066), ~0.8 k is
shared-file behaviour change (`serial.cc` 412, `uc51.cc` 262, `pca.cc` 84,
`wdt.cc` 17, `timer0.cc` 7), and ~0.15 k is build/identity plumbing
(`stypes.h`, `globals.cc`, `sim51.cc`, `regs51.h`, `objs.mk`, `Makefile`).

## 3. Proposed order

Rules that drive the ordering: a change that alters behaviour for a part nobody
reported must not share a change with a new device; a device is only useful once
it has an identity in `-t`/`-H`; and a peripheral that needs a clock scale needs
the contract that carries the scale.

**Step −1 — make the test harness run (nothing else can be gated without it).**
`make -C src test` currently no-ops (see PR.md: `PKGS_TEST` loses the `sims/`
prefix; `test-lib.mk` is referenced one directory too high), and the checked-in
`src/sims/s51.src/test/config/baseline/*` files already differ from HEAD's own
output in 14/14 files. Until both are true, no contributor — including this
port — can show "no baseline moved" from the project's own facilities. Small,
self-contained, and independently valuable.

**Step 0 — the timer-2 `cycles` contract.** Done: `timer2-cycles.patch`. No
dependencies, no baseline movement, and it is the precondition for steps 4 and 6
(both need a per-peripheral clock scale that reload/capture currently refuse to
read).

**Step 1 — general peripheral fixes, one PR each, each with its own proof.**
Order by blast radius, smallest first:

1. `wdt.cc` (WDTPRG period; 16/1).
2. `timer0.cc` (6/1).
3. `pca.cc` (83/36) — biggest of the three and the one with real semantics
   (capture on edge, `ECOM|MAT` compare, high-speed toggle, module-4 watchdog);
   it is also the change that a non-Atmel user can hit today, so it deserves
   the most evidence.

Do **not** bundle these with any device work. Each needs a statement of which
existing parts change behaviour, plus a before/after transcript in the shape of
`evidence/timer2_cycles_demo.py`.

**Step 2 — `regs51.h` additions.** Pure addition (104/0) of the RD2 SFR
addresses and bit masks (`CKCON0 0x8f`, `CKRL 0x97`, `WDTPRG 0xa7`,
`CKCON1 0xaf`, `IEN1 0xb1`, `BRL 0x9a`, `BDRCON 0x9b`, `KBLS/KBE/KBF 0x9c-0x9e`,
the SPI block, and ~50 bit masks). Ships alone or with step 3; it changes no
behaviour, so it is the cheapest way to get the vocabulary into the tree. The
caveat is a real one: those addresses are already occupied in `uc51.cc`'s
`sfr_tab51` by other families — `0x97` `C0RMS1` (`CPU_DS390|CPU_DS390F`, line
85), `0x9b`-`0x9e` `ESP/AP/ACON/C0TMA0` (88-91), `0xa7` `C0RE` (99), `0xaf`
`C0M5C` (106), and on `CPU_F380` `0x8f` `PSCTL`, `0x97` `USB0DAT`, `0xb1`
`OSCXCN` (217-248). Because step 3 puts `CPU_89C51RD2` **inside** `CPU_ALL_52`,
which already contains `CPU_DS390` and `CPU_F380`, every new table row must be
gated on `CPU_89C51RD2` alone, or those parts gain a second name at the same
address.

**Step 3 — the atomic device unit.** Must land as one change; split any of these
four and the tree is broken or the part invisible:

* `stypes.h`: `CPU_89C51RD2`, **and** its insertion into `CPU_ALL_52` — the fork
  records this as a named trap: the SFR and bit name tables are gated on that
  composite, so a part left out boots with no SFR names at all, printing bare
  addresses with no diagnostic.
* `globals.cc`: the `89C51RD2` / `89C51ED2` rows.
* `sim51.cc`: `mk_controller()` case constructing `cl_uc89c51rd2`.
* `uc89c51r.cc` / `uc89c51rcl.h`: a minimal model (clone of `cl_uc89c51r` plus
  the RD2 register set), **plus** the `sfr_tab51` / `bit_tab51` rows from
  `uc51.cc`.

Recommended scope cut: keep the `do_interrupt()` / `priority_of()` hunks out of
step 3 unless they are required for the RD2 vectors, and if they are, argue them
as their own step-1-style fix.

**Step 4 — CKRL clock prescaler.** Strictly after step 3: it is an RD2-only
feature (`PCER`/`XRS2` gated), and it is a clock-scale feature, so it also
depends on step 0. Note the shape the model forces: `cl_uc::tick_hw()` delivers
`hw->tick(1)` one call at a time (`uc.cc:2947`), so a divider whose factor
exceeds 2 cannot be expressed as "counts per call" — it needs phase carried
across calls, and a test that pins no drift over a long window at the maximum
divider. Fix the bypass endpoint first (`CKRL = 0xFF` = divide by 1, never divide
by zero) and pin the reset value from the datasheet rather than from the current
model.

**Step 5 — new peripherals, one PR each**, in ascending size and ascending
coupling to the CPU: `rd2_brg` (198 lines, needs only `T2CON`-adjacent registers
and a tick), then `rd2_kbd` (370, needs its own interrupt vector — which is
exactly the vector at `003Bh` that the fork's STC guard in `interrupt.cc` frees,
a coupling worth stating to upstream), then `rd2_boot` (559, boot-ROM remap: it
moves the vector table and therefore every trace of the part), then `rd2_spi`
(939, and the only one that realistically wants a `cl_hw` simulation-interface
story for a host-side peer). Each: new files + `regs51.h` rows + `uc89c51r.cc`
wiring + `objs.mk` / `Makefile`.

**Step 6 — the EUART changes in `serial.cc` (374/38). Last, and maybe never.**
This is the only item that changes hardware shared by *every* 51 device in the
tree, across ten methods including `tick()` and `serial_bit_cnt()`, and its
fixed-rate-unit change alters baud timing for parts nobody has complained about.
Options for upstream: (a) upstream-identical defaults with the RD2 branch behind
the `CPU_89C51RD2` bit and a device-conditional rate source; (b) factor the RD2
EUART into a subclass of `cl_serial` (as `serialcl.h`'s 37 added lines already
hint) so `serial.cc` itself barely changes; (c) leave it downstream. This needs a
design conversation with the maintainer before any code is written — do not open
the sequence with it.

## 4. Standing blockers for the whole programme

* **No timing tests exist.** The s51 suite is static configuration dumps
  (`conf; info reg; info mem`); nothing in the tree asserts a counter value or a
  rate. Steps 0-6 each need a new assertion mechanism (pre-built `.ihx` fixtures
  are explicitly what `src/test-lib.mk` asks for: *"Ideally pre-build tests and
  ship the .ihx - we are NOT testing tool-chains!"*), or they will be argued from
  console transcripts forever. `sdcc`/`sdas8051` are not installed on this host,
  so fixtures must be checked in as bytes.
* The `EV_PORT_CHANGED` double notification (`port.cc:191` and `port.cc:247` fire
  for a single latch write) will contaminate every pin-driven peripheral test in
  steps 5 and 6; worth its own upstream report before the SPI/keyboard work.
* **Nothing here may be pushed.** This directory is a patch drop; opening issues
  or PRs upstream is the maintainer's/user's call, and no remote was contacted
  beyond `git clone` plus read-only GitHub API reads.
