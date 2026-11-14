# Symbol loading (`sym`) — implementation note

Suite: `t/symtest.py` (registered in `t/floor.sh`). Fixtures:
`t/symtest.sym`, `t/symtest_narrow.sym`, `t/symtest_bad.sym`.

## What already existed, and was reused

Everything about *using* a name already worked; only the *loader* was missing.

* `cl_var_list` (`core/sim.src/varcl.h`) keeps two indexes, `by_name` and
  `by_addr`, of `cl_cvar`; `cl_var`/`cl_var_instance` are the cell-carrying
  form. Every entry carries `defined_by` from `enum var_by { VBY_PRE, VBY_USER,
  VBY_DEBUG, VBY_ANALYZE }`. The `.cdb` and `.map` loaders already stamped
  `VBY_DEBUG` on what they bound; so does this one.
* `cl_uc::symbol2address` / `symbol2cell` / `get_var` (`core/sim.src/uc.cc`)
  resolve a name to a `(memory, address)` cell.
* The command lexer turns any identifier into a cell: `core/cmd.src/cmdlex.l`
  rule `\.?[a-zA-Z_][0-9a-zA-Z_]*($[0-9]+)?` calls `uc_yy_check_id`, which asks
  `uc->memory()`, then `uc->symbol2address()`, then `vars->by_name`. So
  `break <name>`, `expression <name>` and a breakpoint's `if <name>` condition all
  take names for free: the pre-change binary stops with `(104)` on
  `break _load_acc` once `var _load_acc rom 0x3a` has been typed by hand, and
  `bpcmdtest` already fires `break 0x3a if z==0x5a` through a hand-bound name.
* `cl_uc::read_cdb_file` (SDCC's CDB records) and `cl_uc::read_map_file`
  (linker map) exist and were not touched by this work. `read_file` already
  looked for a `.cdb` and a `.map` sibling of a loaded image. (`read_map_file`
  has since grown a second parser for what current `sdld` prints; see
  `t/maptest-notes.md`.)
* `valid_sym_name()` (`core/utils.src/utils.h`) rejects names the console could
  never type.

## What was added

| where | what |
|---|---|
| `core/sim.src/uccl.h` | `virtual long read_sym_file(cl_f *, cl_console_base *con= NULL)` and `virtual class cl_memory *sym_memory(const char *area, t_addr addr)` |
| `core/sim.src/uc.cc` | `cl_uc::read_sym_file` (parser + binder, ~1975-2350), default `cl_uc::sym_memory` (returns `rom`), `.sym` sibling block in `read_file` beside the `.cdb`/`.map` ones, registration of `sym` in `cl_uc::cmds()` |
| `core/cmd.src/cmd_uccl.h`, `cmd_uc.cc` | `cl_sym_cmd` (`sym "FILE"`), and a guard in `cl_var_cmd`: a bare `var name` on a name a symbol file placed keeps that cell instead of allocating a slot of `uc->variables` |
| `sims/s51.src/uc51cl.h`, `uc51.cc` | `cl_51core::sym_memory`: which of rom/iram/sfr/xram a symbol's area means |

`read_sym_file` deliberately does not know what an 8051 is: it asks the model
through `sym_memory(area, value)`, because the same `0x30` is an instruction in
`CSEG` and a variable in `DSEG`. Bit-area (`.bit` / `BSEG`) symbols return
`NULL`: their value is a bit number, while the `bits` space here is indexed by
byte, so there is no cell to hand out; they are reported, not bound.

## Format assumptions

`.sym` is produced by the ASxxxx assembler that sdcc drives, not by sdcc:
`sdas/asxxsrc/asmain.c` opens it under `-s` (`if (sflag) tfp= afile(q,"sym",1)`)
and calls `lstsym(tfp)`; the layout comes from `lstsym()` in
`sdas/asxxsrc/aslist.c`:

* banner `ASxxxx Assembler ... (Intel 8051), page N.` repeated per page;
* a `Symbol Table` heading, then records
  `" %2X "` area reference (4 blanks when the symbol is absolute),
  `"%-8.8s"` name (`"%-60.60s"` under `-w`), `'='` if equated else blank,
  `"  %04X "` value (`" **** "` when unplaced, wider for 3/4-byte values), then
  the flags `G` global, `L` local, `R` relocatable, `X` undefined;
* without `-w` three records share a line separated by `"| "`, with `-w` one per
  line — both shapes have a fixture;
* an `Area Table` heading, then rows `"%3X %-8.8s   size %4X   flags %4X"`, where
  the flags are `aslink.h`'s `A_*` bits (`A3_ABS` is octal 010).

Parsing is right-anchored: the value column is the only field whose width the
assembler keeps fixed, while the name column width depends on `-w`, and a name
longer than the column simply overflows it.

**sdcc and sdas8051 are not installed on this machine**, so no real `.sym` could
be generated here; the fixtures are hand-written to the layout above. The column
widths, the per-page banner and the fact that these tables contain the SFR names
(`A`, `ACC`, `IE`, `P0`) were checked against a real sdas8051-produced
`main.sym` for 8051 firmware fetched over the network.

## Decisions

**Precedence: the device and the user beat the file.** A name that already has a
binding made by anything other than a symbol file (`VBY_PRE` device registers and
bits, `VBY_USER` `var`, `VBY_ANALYZE`) is left alone and merely reported; a name
a previous symbol file bound (`VBY_DEBUG`) is replaced, so rebuilding and
reloading refreshes the table. Reasons: `info variables`, `expression ACC` and
the disassembler print those names, and `ident89c51r` pins those transcripts
byte for byte; a `.sym` row states only what the assembler called something, not
what it is; and refusing the file outright would be worse, because colliding SFR
names are normal in real sdcc output while the other two hundred names are the
reason for the load. The suite pins both halves — the fixture claims `ACC` is the
byte at `iram 0x30`, which would be visible as 65 instead of 90, and `ACC` must
still read the accumulator.

**A broken line stops the load**, with `sym: FILE:LINE: reason, nothing after it
is loaded`. Half a table silently mistaken for the whole one is the failure mode
that would waste the most time later.

**Relocatable symbols are bound at the value in the file and the load says so.**
A `.sym` records area *references* and the area *names* but no area bases — only
the linker knows where it put `CSEG`. So `_main` in `CSEG` at `0030` lands at
`rom[0x30]`, which is right for the single-module case the fixtures model and
wrong for a linked multi-area build; the warning names the count so the reader
goes to the `.cdb`/`.map` instead of trusting it.

**`file "IMAGE"` loads `IMAGE.sym` beside it**, exactly as it already loads
`IMAGE.cdb` and `IMAGE.map` (and with the same quirk inherited from those two:
the sibling is only tried when the image name had to be resolved, i.e. typed
without its extension).

## Traps found on the way

* `chars::rtrim()`/`rrip()` only write NULs — they do **not** shorten the cached
  `chars_length`, so `len()` and `c()` keep reporting removed columns, while
  `rrip(n)` counts its `n` from the stale length and wipes the wrong bytes. The
  record parser therefore works on the raw buffer from `seg.str()` with an
  explicit length.
* `chars::trim()` is `ltrim()`+`rtrim()`; applying it to a whole record line eats
  the leading blanks of the first record, and that leading column is the area
  reference. The line is split as it stands and only the headings are trimmed.
* Console output interleaves with the echoed command stream, so no check in the
  suite anchors a message at the start of a line.
* `-q` suppresses the `N symbols read from` count; the suite keys on the
  diagnostics `read_sym_file` prints itself.
* A real `.sym' is not one table. `lstsym()' breaks the page (`slew()' in
  `asxx/aslist.c': a form feed with the version line ending `Page N', then the
  radix line, a timestamp and the section title again) - in t/map/tiny.sym that
  break falls at line 61 - and every hand-written fixture in this suite fits on
  one page. So since the `.sym' loader shipped, both this loader and
  `LoadSym51.java' refused the second page of any real build at its banner
  (`sym: tiny.sym:61: `2' is not an address') and bound nothing after it. An
  agreement gate made of one-page fixtures passed the whole time. The header is
  now recognised on both sides, and the last leg of `symmaptest' is the gate.
* An area whose name says external RAM is external whatever its address says.
  sdcc links XSEG at 0x0001 (`-b XSEG = 0x0001' in its own link command), so the
  first `__xdata' object of every real build sat where the address ladder
  answered `iram` - a symbol pointing at the wrong memory, which is the one
  thing a symbol loader must not do on a machine where MOV and MOVX are the
  difference. `lkmem.c`, which sizes `XRam' from the two area names, is the
  authority for the rule.

## What upstream would need

* A `.map' reader for what current `sdld' prints: **done**, see
  `t/maptest-notes.md`. `read_map_file` used to match only the legacy ASlink
  layout (`Area / Addr / Size` headings and `C:`-prefixed symbol rows, the
  `s_wait_area`/`s_wait_seginfo` state machine), which modern `sdld` output
  matched in almost no line: measured, a wide map bound 8 names - the ROM
  symbols that happened to carry a page prefix, all to `rom`, each with the
  leading `_` stripped - and a narrow map bound none, silently.
* Area bases, if a `.sym` is ever to place linked builds on its own: either read
  the bases from the map, or let the user set them (`var` per symbol is the
  workaround today).
* `sym_memory` is a new virtual on `cl_uc`; other model families (AVR, MCS
  Core) get the base implementation, which assumes code. Each family that has
  more than one address space should override it the way `cl_51core` does.
* A `\.sym` entry in the `file` command's documentation of siblings, and the
  `sym` command in `docs/`, since `docs/ucsim.texi` keeps its command tables by
  hand.
