# Linker-map loading (`file IMAGE` + `IMAGE.map`) — implementation note

`cl_uc::read_map_file()` (`ucsim/src/core/cmd.src/cmd_uc.cc` area, shared core) has
always existed, but it only recognised what ancient ASlink printed: `Area / Addr /
Size` headings and `C:`-prefixed symbol rows, driven by the `s_wait_area` /
`s_wait_seginfo` state machine. What `sdld` prints today matched that machine in
almost no line, so `file "prog"` beside a modern map bound names *silently wrongly*
or not at all: measured on `t/map/tiny-wide.map` it bound 8 names — only the ROM
symbols that happen to carry a page prefix, all of them to `rom`, each with the
leading `_` stripped — and on the 3-column layout it bound none, with no word of
refusal. The wide map is 14 KB, so that silence was not a small file being skipped.

`t/maptest.py` (10 legs) is the gate; this file is the provenance and the reasoning.

## What the reader does now

`read_map_file` no longer parses the file directly. It drains it once into a
`cl_list` and then dispatches:

* A line holding ` bytes (` is a `sdld` map area row, and the file's format is
  settled by whether `Global Defined In Module` also appears. If it does,
  `read_sdld_map_file()` (new, `ucsim/src/core/uc.cc`) handles the file; if not, the
  legacy state machine runs over the same list, rules untouched, so ASlink-era maps
  behave exactly as before. A file that binds nothing and has the `sdld` shape gets
  an explicit line telling the user to link with `sdld -w`.

Draining first rather than reopening is forced, not stylistic: the `cl_f` that
`mk_io` builds for the map has no name, and `cl_f::close()` wipes the name it does
have, so any "scan the header, then reopen" plan reads garbage. Measured.

`read_sdld_map_file` recognises two rows:

* an **area** row — `  B XSEG    size      3   bytes (XSEG)` — recognised by
  `w[3]=="=" && is_addr(w[1],w[2]) && w[5]=="bytes" && w[6][0]=='('`, which
  remembers the area name for the symbols that follow;
* a **symbol** row — an optional `C:`/`D:` page prefix, then an 8-hex absolute
  address, then the name, then the module it came from.

A symbol binds when the module column is non-empty (a row without one is an
`ASlink-` mark like `l_DSEG` or an unresolved `******` global) and the name does not
start with `.`. Names go in **verbatim** — the legacy machine stripped a leading `_`,
which renames every C symbol on the chip's own tool chain, and `_main` is the name a
user types. The memory comes from `sym_memory(area, addr)`, the same single authority
the `.sym` loader uses, so `.sym` and `.map` cannot disagree about a name. An
existing binding is never clobbered; the kept and clash counts are reported.

## The fixtures are linked, not typed

`t/map/` holds the output of a real tool chain over one 20-line C file
(`tiny.c`): one object in each address class — `__code` function and table, `__data`
variable, `__xdata` array, initialised `__xdata` array, and a `__bit` — built with

    SDCC : mcs51/... TD- 4.6.0 #16555 (Linux)
    ASxxxx Assembler V05.50.4+NoICE+SDCCmods-WIP-R14  (Intel 8051)
    ASxxxx Linker V05.50.4-SDLD

and verified reproducible from `tiny.c` byte for byte:

    sdcc -mmcs51 -S tiny.c
    sdas8051 -w -s -o tiny.rel tiny.asm        # tiny.rel + tiny.sym
    sdld -w -m -i -b "HOME = 0x0000" -b "XSEG = 0x0001" -b "BSEG = 0x0000" \
         -k $SDCC/share/sdcc/lib/small -l mcs51 -l libsdcc -l libint \
         -l liblong -l libfloat tiny.rel       # tiny.ihx + tiny.map  -> tiny-wide.map
    sdld -m -i -b "HOME = 0x0000" -b "XSEG = 0x0001" -b "BSEG = 0x0000" \
         -k $SDCC/share/sdcc/lib/small -l mcs51 -l libsdcc -l libint \
         -l liblong -l libfloat tiny.rel       # the same, 3-column  -> tiny-narrow.map

`t/map/tiny.sym` equals the assembler's output, both `.map` files equal the linker's,
and **the two `.ihx` files are byte-identical** — the whole difference between the
wide and narrow case is one `-w` on the linker, so any behavioural difference is the
reader's, not the program's. `-w` on the *assembler* matters for the `.sym` too:
without it `lstsym()` prints three records per line and the committed one-page-per-
record fixture does not reproduce.

Why the narrow layout is refused rather than parsed: with three records per line the
fields are `  17 _code_ne   000000 GR` — the linker truncates the *name* column too,
so `_code_near` arrives as `_code_nea`. A loader that bound those names would put
`_code_nea` on the address of `_code_near`: a symbol pointing at the wrong place,
which on this machine is worse than no symbol. `sdld -w` is one flag, and the
refusal says so.

## What the wide map actually binds

`file "tiny"` against the wide map, read out of `info variables`:

    _main                        rom[0x00d0]     __sdcc_program_startup  rom[0x0049]
    _code_near                   rom[0x00aa]     sdcc_atomic_compare_exchange_gpt   rom[0x003f]
    _table                       rom[0x00d9]     sdcc_atomic_exchange_gptr_impl     rom[0x002f]
    _counter                     iram[0x08]      sdcc_atomic_exchange_rollback_en   rom[0x002f]
    _xvar                        xram[0x0001]    sdcc_atomic_exchange_rollback_st   rom[0x0008]
    _xinit                       xram[0x0004]
    __start__stack               iram[0x21]

12 names, with `map: 72 row(s) are linker marks or unresolved globals, not symbols`
accounting for the rest of the file. `_table` is in `CONST`, `_xinit` in `XISEG`,
`_counter` in `DSEG` — the memory comes from the area, never from the number.

Notes on what the linker writes:

* The name column is 32 characters wide, so `sdcc_atomic_compare_exchange_gptr_impl`
  reaches the reader as `sdcc_atomic_compare_exchange_gpt`. Two C names that agree
  past character 32 collide in the map; the loader keeps the first and counts the
  second as a clash rather than moving the name.
* Only **global** symbols are listed. `__sdcc_program_exit` is local to the module,
  so it is in `t/map/tiny.sym` and absent from every `.map`.
* `__bit` objects appear in neither file's global list: `_flag` is bound by neither
  loader, which is what the `no bit names in a map` leg pins (the `.sym` does carry
  it, area `BSEG`, where `sym_memory` refuses it — see `t/symtest-notes.md`).
* `file "tiny"` needs the **extension-less stem**. `file "tiny.ihx"` looks for
  `tiny.ihx.map`, which nothing ever produces.

## Three findings this fixture produced

1. **`XSEG` is external RAM whatever its address says.** `sdcc`'s own link command
   says `-b XSEG = 0x0001`, so the first `__xdata` object of every real build sits at
   `0x0001`, where the address ladder in `sym_memory()` answers `iram` — the symbol
   named the wrong memory, on a machine where `MOV` and `MOVX` are the difference.
   `XSEG`/`XISEG` are now decided by name before the ladder, in `cl_51core::sym_memory`
   and mirrored in `gh/LoadSym51.java`; the authority is `lklist.c`/`lkmem.c`, which
   size `XRam` from `EQ(xp->a_id, "XSEG") || EQ(xp->a_id, "XISEG")`. The last leg of
   `t/maptest.py` runs the same names through the `.sym` loader to prove the fix did
   not land in the map reader alone.
2. **`ISEG` counts as internal RAM.** `lkmem.c` adds `ISEG` to `IRam`; it was not in
   `sym_memory`'s `data_areas`, so it fell through to `rom`. Added on both sides.
3. **A real `.sym` runs onto a second page and both tools used to stop there.** The
   invented one-page fixtures hid it for a whole cutover; see the "Traps found on the
   way" section of `t/symtest-notes.md`. Same file, same build: `t/map/tiny.sym` is
   now the multi-page fixture, and leg 11 of `t/symmaptest.py` is the cross-tool gate.

That last gate also surfaced a divergence worth recording here, because it is a
property of the two tools and not of any fixture: sdas8051 emits its built-in SFR and
bit aliases twice, once upper-case and once lower (`RXD` and `rxd`, both absolute,
both `= 0000B0`). ucsim refuses to overwrite a name its cell table already has
case-**sensitively** — it knows `dph` as `dptr[0x1]` — while Ghidra's symbol table
answers case-**insensitively**, so `RXD` is refused there (`already_bound`, it is a
symbol in the language's `BITS` space) while `rxd` is added as a secondary symbol in
`SFR`. Nine names in `t/map/tiny.sym` diverge this way. The leg accepts a
one-sided name only when the other tool knew it *before* the file was loaded and the
row is one of those absolute alias rows, so a linker object can never diverge without
failing the gate.

## The `.sym` alone cannot place a linked build

`t/map/tiny.sym` and `t/map/tiny-wide.map` are the same program, and they do not
agree on addresses: an area-relative `.sym` row is

      B _xvar                              000000 GR

which is an **offset inside `XSEG`**, and the `.sym` prints no area bases at all.
Both tools therefore bind `xram[0x0000]` from the `.sym` and `xram[0x0001]` from the
map, and both are reading their own file correctly. The fix is not in the loaders:
either the bases are read from the map alongside the `.sym`, or the user sets them
(`var` per symbol is the workaround today). That is the remaining item in
"What upstream would need" below, and it is why `symmaptest` asserts memory and
printed value rather than address when the fixture is a `.sym`.

## What upstream would need

* `read_sdld_map_file` is additive: the legacy path is byte-for-byte the old rules
  over a `cl_list`, and the dispatch is one sniffed line. Both layouts are in
  `t/map/` so a change to either is caught by `t/maptest.py`.
* Area bases, so a `.sym` can place a build by itself (see above).
* `docs/ucsim.texi` keeps its command tables and its list of `file` siblings by hand:
  the `.map` sibling and its two accepted layouts belong there.
* A `--map`/`-m` command-line option would beat sibling-by-name discovery, which
  today requires the extension-less stem and silently does nothing when it is absent.
