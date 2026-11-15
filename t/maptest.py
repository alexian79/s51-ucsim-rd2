#!/usr/bin/env python3
"""What a linker map does to the console's names: `file IMAGE' loading IMAGE.map.

The fixtures come off a real tool chain, not out of a head: t/map/tiny.c assembled by
sdcc 4.6.0's sdas8051 and linked by its sdld (ASlink V05.50.4-SDLD) twice, once with
`-w' (t/map/tiny-wide.map, the layout sdcc's own link step asks for) and once without
(t/map/tiny-narrow.map, the 3-column default). The `.ihx' is byte-identical between the
two links, so every case below runs the same program and only the map differs. The
source deliberately holds one object per address class - a code function, `__xdata'
with and without an initialiser, `__data', `__code' - so each binding is checkable
against the class the linker put it in.

Before this reader, `file tiny' against those files bound 8 names on the wide map - the
ROM symbols that happened to be printed with a `C:' page prefix, bound to rom whatever
their area said and with the leading `_' stripped off the name, which is a different
name from the one `sym' binds for the same object - and 0 names on the narrow map, in
silence. The wide map is 14 KB, so the silence was not a small file being skipped.

The two rules the legs pin, because they are the two that were wrong:

  * the memory comes from the AREA of the row, through the same `sym_memory()' the
    `.sym' loader uses. `_xvar' in XSEG is external RAM at 0x0001 - sdcc links XSEG at
    0x0001 by default, so classifying by address alone puts `__xdata' objects in
    internal RAM, which is the single most expensive mistake a symbol loader can make
    on an 8051, since MOVX and MOV pick the memory;
  * a name is bound only if the row carries a module column, which is how ASxxxx says
    "global defined in this module". The linker's own marks (`l_', `s_', `b_', `p_')
    have no module column, so they are refused without needing a list of their names.

Narrow maps are refused with a message rather than parsed: that layout truncates names
to 9 characters (`_code_nea') and drops the module column, so binding it would mint
names that match nothing else in the tool chain.
"""
import atexit
import os
import re
import shlex
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simproc import run_pipe

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S51 = os.environ.get("S51") or HERE
SIM = os.environ.get("S51_SIM") or os.path.join(
    S51, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
# The fixtures live beside this suite.  $S51 selects the *vendor* tree, and the two are
# different directories whenever the floor is run against another checkout -- which is
# exactly how this suite broke once $S51 had to be set so that runtests could find src/.
MAPDIR = os.path.join(HERE, "t", "map")
IHX = os.path.join(MAPDIR, "tiny.ihx")

MAIN = 0x00d0                   # CSEG, `C: 000000D0  _main'
CODE_NEAR = 0x00aa
TABLE = 0x00d9                  # CONST, i.e. code
COUNTER = 0x08                  # DSEG
XVAR = 0x0001                   # XSEG, page 0: the row carries no `D:' prefix
XINIT = 0x0004                  # XISEG


def case(kind):
    """A scratch directory holding the image plus the sibling under test, because
    `file "tiny"' only looks for `tiny.map' / `tiny.sym' beside the image when the
    name had to be resolved - typed without its extension."""
    d = tempfile.mkdtemp(prefix="maptest-%s-" % kind)
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    shutil.copy(IHX, os.path.join(d, "tiny.ihx"))
    if kind == "wide":
        shutil.copy(os.path.join(MAPDIR, "tiny-wide.map"),
                    os.path.join(d, "tiny.map"))
    elif kind == "narrow":
        shutil.copy(os.path.join(MAPDIR, "tiny-narrow.map"),
                    os.path.join(d, "tiny.map"))
    elif kind == "sym":
        shutil.copy(os.path.join(MAPDIR, "tiny.sym"),
                    os.path.join(d, "tiny.sym"))
    return os.path.join(d, "tiny")


WIDE = case("wide")
NARROW = case("narrow")
NOMAP = case("nomap")
SYMONLY = case("sym")

F_WIDE = 'file "%s"' % WIDE
F_NARROW = 'file "%s"' % NARROW
F_NOMAP = 'file "%s"' % NOMAP
F_SYM = 'file "%s"' % SYMONLY


def run(cmds, timeout=30):
    """Drive the console the only way that survives `run' freezing it: hold stdin
    open a moment past the last command. `-q' is what a user of the floor expects
    and it also suppresses the `N symbols read' count, so every leg below keys on
    the bindings and on the diagnostics the loader prints regardless of quiet."""
    quoted = " ".join(shlex.quote(c) for c in cmds)
    pipe = '( printf "%%s\\n" %s; sleep 1 ) | %s -t 89C51R -q -c -' % (
        quoted, shlex.quote(SIM))
    # run_pipe, not subprocess.run: a timeout has to kill the simulator `sh'
    # spawned, not only `sh' -- see t/simproc.py.
    res = run_pipe(["sh", "-c", pipe], timeout)
    return "<<timeout>>" if res.killed else res.out


def has(out, needle):
    """Substring search on purpose: the echoed command stream interleaves with the
    simulator's own output, so a message may not begin a line."""
    return needle in out


def var_row(out, name):
    """Where `info variables' says a name is bound, or None."""
    for line in out.splitlines():
        m = re.match(r'^%s\s+(\S+)\s*=' % re.escape(name), line.strip())
        if m:
            return m.group(1)
    return None


def binds(name, want):
    def check(out):
        got = var_row(out, name)
        if got != want:
            return "%s bound at %r, not %r" % (name, got, want)
        return None
    return check


def unbound(name):
    def check(out):
        got = var_row(out, name)
        if got is not None:
            return "%s should not be bound, is at %r" % (name, got)
        return None
    return check


def and_(*checks):
    def check(out):
        problems = [p for p in (c(out) for c in checks) if p]
        return "; ".join(problems) if problems else None
    return check


# --- the checks ----------------------------------------------------------------

def wide_code_rows(out):
    """Code areas are rom, whatever the area is called: CSEG for the functions,
    CONST for the `__code' array."""
    return and_(binds("_main", "rom[0x%04x]" % MAIN),
                binds("_code_near", "rom[0x%04x]" % CODE_NEAR),
                binds("_table", "rom[0x%04x]" % TABLE))(out)


def wide_data_row(out):
    """DSEG is internal RAM at its own address."""
    return binds("_counter", "iram[0x%02x]" % COUNTER)(out)


def wide_xdata_rows(out):
    """XSEG and XISEG are external RAM at 0x0001 and 0x0004. Address-based
    classification says iram here, and that is the defect this leg exists for."""
    return and_(binds("_xvar", "xram[0x%04x]" % XVAR),
                binds("_xinit", "xram[0x%04x]" % XINIT))(out)


def names_verbatim(out):
    """`_main' stays `_main', the way `sym' binds it. The legacy reader stripped
    the leading underscore, so the two loaders named one object two things."""
    return and_(unbound("main"),
                binds("_main", "rom[0x%04x]" % MAIN))(out)


def marks_refused(out):
    """The linker's own marks carry no module column, so they are not symbols -
    and the loader says how many it left alone instead of binding 72 of them."""
    for mark in ("l_XSEG", "s_XSEG", "l_CONST", "s_CONST", "l_BSEG"):
        problem = unbound(mark)(out)
        if problem:
            return problem
    if not has(out, "linker marks"):
        return "no count of the skipped marks was printed"
    return None


def no_bit_symbols(out):
    """`__bit flag' is in the `.sym' of this same link and in none of its maps:
    sdld never prints a bit global in an area block, so the `.sym' file stays the
    only source of bit names. Pinning that here keeps anyone from "fixing" the map
    loader by parking a bit number at a byte."""
    return unbound("_flag")(out)


def break_by_name(out):
    if not has(out, "Breakpoint 1 at 0x%06x" % MAIN):
        return "`break _main' did not resolve to the map's value"
    n = len(re.findall(r'Stop at 0x%06x: \(104\)' % MAIN, out))
    if n != 1:
        return "expected exactly one stop at 0x%06x, saw %d" % (MAIN, n)
    return None


def narrow_refused(out):
    """The refusal has to say what to do about it, and it must bind nothing: a
    9-character truncation of `_code_near' is not a name anyone can type."""
    if not has(out, "sdld -w"):
        return "the narrow map was accepted without a word"
    return and_(unbound("_main"), unbound("_counter"),
                unbound("_code_near"), unbound("_code_nea"))(out)


def nomap_is_silent(out):
    """The control that makes the refusal above mean something: with no map at all
    the loader prints no advice, so the message is about the layout, not about
    there being no symbols."""
    if has(out, "sdld -w"):
        return "the no-map case was told to relink with -w"
    return and_(unbound("_main"), unbound("_xvar"))(out)


def sym_path_still_external(out):
    """The same image with only its `.sym': the area rule is `sym_memory()'s' and
    the `.sym' loader goes through it too, so XSEG must still answer xram. This is
    the leg that fails if the fix lands in the map reader alone."""
    got = var_row(out, "_xvar")
    if got is None:
        return "`_xvar' is not bound from the .sym at all"
    if not got.startswith("xram["):
        return "`_xvar' (XSEG) bound at %r, not external RAM" % got
    return binds("_counter", "iram[0x%02x]" % 0x00)(out)


# --- the legs ------------------------------------------------------------------
# (label, exact console command lines, check)

LEGS = [
    ("wide map: code areas", [F_WIDE, "info variables"], wide_code_rows),
    ("wide map: DSEG in iram", [F_WIDE, "info variables"], wide_data_row),
    ("wide map: XSEG at 0x0001", [F_WIDE, "info variables"], wide_xdata_rows),
    ("names keep their _", [F_WIDE, "info variables"], names_verbatim),
    ("linker marks refused", [F_WIDE, "info variables"], marks_refused),
    ("no bit names in a map", [F_WIDE, "info variables"], no_bit_symbols),
    ("break _main fires once", [F_WIDE, "break _main", "run"], break_by_name),
    ("narrow map refused", [F_NARROW, "info variables"], narrow_refused),
    ("no map says nothing", [F_NOMAP, "info variables"], nomap_is_silent),
    (".sym: XSEG still xram", [F_SYM, "info variables"], sym_path_still_external),
]


def main():
    if not os.path.exists(SIM):
        print("maptest: no simulator at %s" % SIM)
        return 2
    for f in ("tiny.ihx", "tiny-wide.map", "tiny-narrow.map", "tiny.sym"):
        if not os.path.exists(os.path.join(MAPDIR, f)):
            print("maptest: no fixture at %s" % os.path.join(MAPDIR, f))
            return 2
    failed = 0
    for label, cmds, check in LEGS:
        out = run(cmds)
        problem = check(out)
        if problem:
            failed += 1
            print("FAIL %-26s %s" % (label, problem))
            print("     $ %s" % " | ".join(cmds[1:]))
        else:
            print("ok   %-26s" % label)
    print("maptest: %s %d/%d legs %s" % (
        "PASS" if not failed else "FAIL", len(LEGS) - failed, len(LEGS),
        "" if not failed else "(%d failed)" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
