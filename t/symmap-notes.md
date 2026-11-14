# `.sym' in Ghidra, and the cross-tool agreement gate

Suite: `t/symmaptest.py` (not registered in `t/floor.sh`; registration is the main
agent's). Ghidra side: `gh/LoadSym51.java`. Simulator side: already pinned by
`t/symtest.py`, unchanged here.

## What was added

| where | what |
|---|---|
| `gh/LoadSym51.java` | `GhidraScript` that parses one `.sym`, resolves each record to an address space by the simulator's rules, creates a symbol per placeable record and prints `SYM`/`SKIP`/`SYMBEFORE`/`SYMALL`/`LOAD`/`SPACE`/`BLOCK` lines |
| `t/symmaptest.py` | the agreement gate: same image, same `.sym`, both tools asked what they named and where, then compared |

Nothing in `vendor/`, `lang/`, `t/floor.sh` or the fixtures was touched. The skewed
fixture the gate needs is written to `/tmp/symmaptest/skew.sym` at run time, so `t/`
never grows a `.sym` beside an image (which `symtest.py` warns would be auto-loaded).

## How the script gets its argument, and how it is run

`-postScript LoadSym51.java /abs/path/to/file.sym` - everything after the script name on
the `analyzeHeadless` command line is a script argument (`getScriptArgs()`), so the
`.sym` path is the script's one argument, exactly as `t/langtest.py` passes `-scriptPath
$S51/gh -postScript <script>` and strips the `LoadSym51.java> ` marker (plus the
` (GhidraScript)` suffix) off every line. With no argument the script only dumps the
symbol table twice, which is the control run.

The suite keeps the `langtest.py` conventions (`S51`, `GHIDRA`, `-noanalysis`, the marker
strip), imports the image once per run under its own name into **one** project
(`/tmp/symmaptest/proj`), and runs the five `analyzeHeadless` sessions strictly one at a
time: `base` (no `.sym`), `wide`, `narrow`, `bad`, `skew`.

## The area -> space mapping, with the real names

`cl_51core::sym_memory()` is copied, and the memories are renamed to what this language
actually calls the same storage. The names come from the script's own `SPACE` lines
(`Blocks51`'s `BLOCK` lines agree) - the language declares

```
RAM CODE INTMEM EXTMEM SFR BITS OTHER
```

and the mapping the gate uses and asserts is

| simulator | Ghidra | chosen by |
|---|---|---|
| `rom` | `CODE` | an area that is neither BSEG nor a data area (`CSEG`, `_CODE`, ...) |
| `iram` | `INTMEM` | data area (`DSEG`, `PSEG`, `DATA`, ...) and value `<= 0x7f` |
| `sfr` | `SFR` | data area and value `<= 0xff` |
| `xram` | `EXTMEM` | data area and value beyond that, if the space reaches it (leg 10) |
| (none) | - | `BSEG` / any area whose name contains `BSEG`: no byte cell, refused |

An empty area field (`.equ`, absolute symbols) is data, so `_sp` at `0081` lands on
`sfr[0x81]`/`SFR:0x81` in both tools. `RAM` and `OTHER` exist in the address factory but
no rule in `sym_memory` can reach them; the script never names a symbol there, and leg 1
of the gate asserts the four mapped spaces are present.

**XRAM and XDATA are blocks, not spaces.** The pspec splits external data into the `XRAM`
block (`EXTMEM:0000-06ff`) and the `XDATA` block (`EXTMEM:0700-ffff`); both live in the
single `EXTMEM` address space, so a symbol at `0x1234` belongs in `EXTMEM` exactly as much
as one at `0x0100`. Leg 10 puts a symbol in each block for that reason. Reading the
`BLOCK` lines is the way to see it: `BLOCK XRAM EXTMEM:0000..EXTMEM:06ff` and
`BLOCK XDATA EXTMEM:0700..EXTMEM:ffff`.

**Trap that leg 10 caught, and it was a real disagreement.** The first version of
`symMemory()` mirrored `xram->valid_address(addr)` as `addr < x.getSize()`.
`AddressSpace.getSize()` is the address *width in bits* (16 for a 16-bit space), not a
reach, so every data symbol past `0xff` fell through to `CODE`: with an `XSEG` fixture the
simulator said `xram[0x1234]` while the script printed `SYM CODE:1234`. The mirror now
asks the space itself (`getAddress()` in a `try`, catching
`AddressOutOfBoundsException`), which is what `valid_address()` means. Reverted in a
scratch copy, the leg fails as it must:
`FAIL external data beyond 0xff external data beyond 0xff disagrees on 3 name(s)`,
`_xd_far: simulator xram[0x1234] != ghidra CODE:0x1234` (and `_xd_near`, `_xd_uscore`),
`FAIL 9/10 legs`, exit 1.

## Which side's answer is which

Neither set is derived from the `.sym` text, so a loader that silently drops a row shows
up as a missing name:

* simulator: `info variables` before and after `sym FILE`; the names present only after
  the load are what the file placed (198 bindings exist before the load, so "absent"
  means something).
* Ghidra: `SYMBEFORE` / `SYMALL`, i.e. the symbol table the tool itself reports; the
  second minus the first is what the file placed.

Both are then compared as `(name, space, offset)` pairs. Measured result for
`t/isa.ihx` + `t/symtest.sym`: both place exactly `_main CODE:0x30`, `_load_acc
CODE:0x3a`, `_counter INTMEM:0x30`, `_flag INTMEM:0x40`, `_sp SFR:0x81`; both refuse
`_bitvar` (BSEG) and `__undefined_sym` (`****`); both keep `ACC` and `IE` because the
device/pspec already binds those names.

## Decisions

**The parse is the simulator's, mirror for mirror.** `sym_area_NR`, `sym_area_field`,
`sym_area_ABS` (`A3_ABS`), the right-anchored record parse (value column last, then the
one-letter flags, then `=`, then the 4-column area reference), the `4/6/8`-digit value
token, the `|` splitting of the raw line, the section headings, the banner test, the
`valid_sym_name()` name rule and the "a refused row stops the file" rule all come from
`core/sim.src/uc.cc`; the memory choice comes from `sims/s51.src/uc51.cc`. Two
consequences that were measured rather than assumed:

* A `.sym` whose Symbol Table is broken before the Area Table never gets its areas read
  (the Area Table is printed *after* the symbols, and the load stops at the bad row), so
  in `t/symtest_bad.sym` the surviving `_before_bad` is bound as *absolute* data -
  `iram[0x41]` in the simulator and `INTMEM:0x41` in Ghidra. Both tools do that, and leg
  9 pins it.
* Binding happens after the whole file, so a row's area can be resolved by an Area Table
  that appears later; the script keeps the rows and resolves them at the end for the same
  reason.

**Refuse, never park.** A BSEG row gets no symbol in either tool: the value is a bit
number, there is no byte cell that stands for it, and a plausible byte is worse than
nothing. Leg 5 requires the refusal on *both* sides (the simulator's `no cell here:
_bitvar` and the script's `reason=no_byte_cell` with `area=BSEG`) so the agreement cannot
be produced by one tool quietly omitting it.

**Names the tool already has win.** The simulator leaves a `VBY_PRE` binding alone and
reports it; the script skips a name that already has a symbol and prints
`reason=already_bound`. Same decision, and leg 7 pins both halves including that `ACC` is
still `sfr[0xe0]`/`SFR:0xe0` afterwards.

**A gate that cannot fail proves nothing.** `t/symmaptest.py --skew` feeds the agreement
leg the run in which `_load_acc` was read from an altered fixture, and the suite then
prints `FAIL 9/10 legs` and exits 1. Without the flag the same altered fixture is used the
other way round: leg 4 gives it to one tool at a time (Ghidra skewed against a clean
simulator, then the simulator skewed against a clean Ghidra) and requires the report to
name `_load_acc` with both addresses.

**Leg 10: external data, and where its area name comes from.** A symbol record's first
four columns are a hex area *reference number* (` %2X ` in `lstsym()` - hence `C _main`,
`B _counter`, `D _bitvar` in the shipped fixtures), and the number becomes a name only via
the Area Table rows printed after the symbols (`  %2X %-8.8s   size %4X   flags %4X`, first
column the same number: `0 _CODE`, `D BSEG`). There is no area-name column on a symbol
row: the string `sym_memory()` matches against `DATA_AREAS` is always looked up by
number. `XSEG` is therefore not a spelling invented for this leg - sdld's AOMF51 area
table (`sdas/linksrc/lkaomf51.c`) declares `{"XSEG", 1}` and `{"XISEG", 1}` with class 1 =
external RAM, and `lkmem.c` sizes `XRam` from `EQ(xp->a_id, "XSEG") ||
EQ(xp->a_id, "XISEG")`; sdcc's mcs51 back end spells the same area `_XSEG`, which is what
the leading-underscore strip in `sym_memory()` is for. The leg writes such a `.sym` to
`/tmp` (never to `t/`, which would put a `.sym` beside an image another suite loads) with
`XSEG` and `_XSEG` areas and values `0x0100`, `0x0200`, `0x1234`, then requires
`xram` from the simulator and `EXTMEM` at the same offset from Ghidra - and fails if
either parks the name in code or refuses it. Measured: `_xd_far xram[0x1234]` /
`SYM EXTMEM:1234`, `_xd_near xram[0x0100]` / `SYM EXTMEM:100`, `_xd_uscore
xram[0x0200]` / `SYM EXTMEM:200`.

**One caveat on that quoted row format, learned the hard way.** `aslist.c` chooses it from
three switches: `xflag` for the number column (hex `  %2X ` at :1268, octal ` %3o `,
decimal ` %3u `), `wflag` for the name (`%-8.8s` at :1278, `%-40.40s` under `-w`), and
`a_bytes` for the size field - and the `LONGINT` branch that prints `%4lX` (:1289) is
compiled only for `__TURBOC__` and `__SC__` (`asxxxx.h:92`, `:100`), so a Linux-built
`asx8051` with the default two-byte addresses emits `   size %4X   flags %4X` (:1319).
Quoting `%4lX` as *the* format is a DOS-ism; nothing in this gate depends on the size
field at all, only on column one being the area reference number.

## What the two tools genuinely differ about

* **Wording of a refused row.** Simulator: `sym: FILE:LINE: no address in the line,
  nothing after it is loaded`, printed once. Script: `bad_line=6 bad_why='no address in
  the line'` on its summary line. Same line, same dropped rows, different stream. The gate
  asserts the behaviour (both stop at 6, both bind only `_before_bad`) and prints both
  strings instead of pretending they match.
* **Primary vs secondary.** `SFR:0x81` already carries the pspec's `SP`, so Ghidra's
  `_sp` is `primary=false`; the simulator has no such distinction and binds `sfr[0x81]`
  regardless. The cell is the same. The gate prints the secondary names (measured:
  `_sp`; `_counter`/`_flag` came out primary because those `INTMEM` cells carry register
  *aliases* only at 0x00-0x1f, and 0x30/0x40 are bare).
* **Counts.** `info variables` reports 198 device bindings, Ghidra's table 364 symbols
  (361 distinct names: `P5` is declared at both `E8` and `C7`, and `Symbols51` shows the
  other duplicates). Any name-collision check is therefore done by name, as the
  simulator's own `by_name` index does it.
* **Repeat names inside one file.** The simulator replaces a name a previous `.sym` bound
  (`VBY_DEBUG` is replaceable); the script deletes its own earlier symbol and re-adds it.
  Neither fixture exercises this, so it is not gated - a `.sym` with two rows of one name
  would land on the second value on both sides.
* **Out-of-range refusal.** The simulator checks `valid_address()` on the chosen memory
  and counts the row as having no cell here; the script catches
  `AddressOutOfBoundsException` and prints `reason=out_of_range`. With this language both
  `CODE` and `EXTMEM` reach `0xffff`, so no `.sym` value can trip it and the branch stays
  ungated - unlike the external-data branch above, which leg 10 now covers.
