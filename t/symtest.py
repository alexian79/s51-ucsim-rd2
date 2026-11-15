#!/usr/bin/env python3
"""Pin what a symbol table does to the console's names: `sym FILE', then
`break <name>', `expression <name>' and a bare `var <name>'.

WHAT THE FORMAT IS, AND WHY THE FIXTURES ARE HANDED WRITTEN
-----------------------------------------------------------
`.sym' is not written by sdcc itself but by the assembler it drives.  asmain.c
opens the file when `-s' was given (`if (sflag) tfp= afile(q,"sym",1)') and hands
it to `lstsym(tfp)', so every column in these fixtures comes from lstsym() in
sdas/asxxsrc/aslist.c:

    ASxxxx Assembler V01.80 ... (Intel 8051), page 1.     banner, repeated per page
    Symbol Table                                          heading
     %2X  %-8.8s %s  %04X %s                              one record: area ref, name,
                                                          '=' if equated, value, flags
    Area Table                                            heading
     %3X %-8.8s   size %4X   flags %4X                    one area per row

`-w' widens the name column from 8 to 60 (the real files people post are of that
shape) and prints one record per line; without it three records share a line,
separated by "|".  Value fields may be 4, 6 or 8 digits, and an unplaced symbol
reads " **** ".  sdcc and sdas8051 are NOT installed on this machine, so
t/symtest.sym (wide), t/symtest_narrow.sym (three per line) and
t/symtest_bad.sym are written by hand to exactly that layout; the column widths
were checked against a real `main.sym' produced by sdas8051 for 8051 firmware,
which is also what showed that these tables contain the SFR names (`A', `ACC',
`IE', `P0') and can therefore try to move them.

WHAT MAY WIN A NAME, AND WHY
----------------------------
A symbol file is an outside description of the firmware; the device model is not.
So a name that already has a binding made by anything other than a symbol file -
the special function registers and the bits the device table declares (VBY_PRE), a
`var' the user typed (VBY_USER), a name the analyzer found (VBY_ANALYZE) - keeps
it, and the load only reports that it kept it.  The reasons, in order:

  * `info variables', `expression ACC' and the disassembler print those names;
    letting firmware rename them would change what the machine appears to be, and
    ident89c51r pins those transcripts byte for byte.
  * a `.sym' row says nothing about what the object IS, only what the assembler
    called it, so it is the weaker statement of the two.
  * refusing the whole file instead would be worse: one colliding SFR name is
    normal in real SDCC output, and the other 200 names are the reason for the
    load.

A name a previous symbol file bound (VBY_DEBUG) IS replaced, so that rebuilding
the firmware and reloading refreshes the table instead of leaving stale cells.
The suite pins both halves: the fixture claims `ACC' lives at iram 0x30, which
would be visible as a different value, and `ACC' must still be the accumulator.

THE TRAPS THAT MAKE A LEG PASS FOR NO REASON
--------------------------------------------
  * count the stop CODE, not `Stop at <addr>': (104) is a breakpoint firing,
    (109) a step landing, (112) an event break.
  * a session whose breakpoint never fires simulates forever, and a simulator
    killed at a timeout loses its buffered output, which reads exactly like "no
    breakpoint was created".  Sessions that must terminate carry the sentinel
    `break 0x44' past the target, and the control leg (same `break <name>' with
    nothing loaded) is what proves the name is the thing that worked.
  * the simulator writes its messages into the same stream as the echoed command
    lines, so a message can start in the middle of a line.  Nothing here anchors
    a search at the start of a line.
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
IMG = os.path.join(HERE, "t", "isa.ihx")   # fixtures live beside the suite
T = os.path.join(HERE, "t")


def fixture(name):
    path = os.path.join(T, name)
    if not os.path.exists(path):
        print("symtest: no fixture at %s" % path)
        sys.exit(2)
    return path


F = 'file "%s"' % IMG
SYM = 'sym "%s"' % fixture("symtest.sym")
SYMN = 'sym "%s"' % fixture("symtest_narrow.sym")
SYMB = 'sym "%s"' % fixture("symtest_bad.sym")

# t/isa.ihx: 0x30 is `_main' (the first instruction of the program body), 0x33 is
# `MOV A,#0x5a', 0x3a `MOV A,0x41' is crossed exactly once, and iram[0x30] holds
# 0x41 (65) without any help from the program.  The fixture points `_load_acc' at
# 0x3a, `_main' at 0x30 and `_counter' at iram 0x30, so each of those constants is
# checkable against the numeric form.
TARGET = 0x3a
COUNTER = 65                     # iram[0x30] before the program touches it
ACC_AT_TARGET = 90               # 0x5a, loaded by 0x33 before 0x3a
SENT = "break 0x44"              # sentinel, see the docstring


def run(cmds, timeout=25):
    """Drive the console the only way that survives `run' freezing it: hold stdin
    open a moment past the last command."""
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


def bp_rows(out, addr=None):
    """The rows of an `info break' table, optionally only those at `addr'."""
    rows = []
    for line in out.splitlines():
        m = re.match(r'^(\d+)\s+(fetch|event|read|write)\s+(\S+)\s+(\d+)\s+(\d+)\s+'
                     r'(0x[0-9a-f]+)\s+(\S+)', line.strip())
        if m:
            rows.append(dict(num=m.group(1), type=m.group(2), hit=int(m.group(4)),
                             addr=m.group(6), text=line.strip()))
    if addr is not None:
        rows = [r for r in rows if int(r["addr"], 16) == addr]
    return rows


def fired(out, addr=TARGET, code=104):
    """Stops with `code' at `addr'; the address belongs in the count because the
    session also carries a sentinel breakpoint."""
    return len(re.findall(r'Stop at 0x%06x: \(%d\)' % (addr, code), out))


def values(out):
    """The answers of the `expression' commands in the session, in order.  Only a
    bare number on a line is one: the register dump lines carry spaces."""
    return [int(line.strip()) for line in out.splitlines()
            if re.match(r'^-?\d+$', line.strip())]


# --- the checks ----------------------------------------------------------------

def binds(name, want):
    def check(out):
        loc = var_row(out, name)
        if loc is None:
            return "no binding for %s in `info variables'" % name
        if want not in loc:
            return "%s bound to %s, expected %s" % (name, loc, want)
        return None
    return check


def unbound(name):
    def check(out):
        loc = var_row(out, name)
        if loc is not None:
            return "%s bound to %s, expected no binding at all" % (name, loc)
        return None
    return check


def wide_fixture(out):
    """The three shapes a `.sym' row comes in: code, data, and an absolute
    equate, each of which has to land in a different address space."""
    for name, want in (("_main", "rom[0x0030]"), ("_load_acc", "rom[0x003a]"),
                       ("_counter", "iram[0x30]"), ("_sp", "sfr[0x81]")):
        problem = binds(name, want)(out)
        if problem:
            return problem
    return None


def narrow_fixture(out):
    """Three records to one line, separated by `|', 8-column names."""
    for name, want in (("_main", "rom[0x0030]"), ("_acc2", "rom[0x003a]"),
                       ("_cntr", "iram[0x30]")):
        problem = binds(name, want)(out)
        if problem:
            return problem
    return None


def refused_rows(out):
    """The rows the assembler could not place (****), or that name a bit rather
    than a cell: reported, and bound to nothing."""
    if not has(out, "no address"):
        return "the undefined symbol was skipped without saying so"
    if not has(out, "no cell here"):
        return "the bit-area symbol was skipped without saying so"
    return unbound("__undefined_sym")(out) or unbound("_bitvar")(out)


def bad_line_refused(out):
    """The line number has to be in the message, the row before it has to be
    bound and the row after it must not be."""
    if not re.search(r"symtest_bad\.sym:6\b", out):
        return "refused without naming line 6: %r" % out.splitlines()[:12]
    if not has(out, "nothing after it is loaded"):
        return "refusal does not say that the rest of the file is dropped"
    return (binds("_before_bad", "iram[0x41]")(out)
            or unbound("_after_bad")(out))


def break_by_name(out):
    if not bp_rows(out, TARGET):
        return "no breakpoint at 0x%06x, so the name resolved to nothing" % TARGET
    if fired(out) != 1:
        return "expected one (104) at the target, got %d" % fired(out)
    return None


def break_without_symbols(out):
    """The control: the same command with no symbol file must create nothing, or
    the `break by name' leg would pass on an address typed by luck."""
    if bp_rows(out, TARGET):
        return "a breakpoint exists at the target although nothing was loaded"
    if fired(out):
        return "stopped at the target with no symbol file loaded"
    return None


def data_name_matches_number(out):
    """`expression _counter' and `expression iram[0x30]' are the same cell, and
    both have to say so: a name that resolved to nothing answers 0, which is what
    makes the constant below part of the check."""
    got = values(out)
    if len(got) < 2:
        return "no values came back: %r" % got
    if got[0] != COUNTER or got[1] != COUNTER:
        return "_counter=%s iram[0x30]=%s, expected %d and %d" % (
            got[0], got[1], COUNTER, COUNTER)
    return None


def sfr_not_shadowed(out):
    """The fixture claims `ACC' is the byte at iram 0x30.  Stopped at 0x3a the
    accumulator holds 0x5a and that byte holds 0x41, so a shadow is worth 90
    against 65 and cannot be missed."""
    if not has(out, "kept it: ACC, IE"):
        return "the load did not report that ACC and IE kept their bindings"
    loc = var_row(out, "ACC")
    if loc != "sfr[0xe0][7:0]":
        return "ACC bound to %s, expected the accumulator cell" % loc
    got = values(out)
    if len(got) < 2:
        return "no values came back: %r" % got
    if got[0] != ACC_AT_TARGET:
        return "ACC reads %d, expected %d (the fixture moved it)" % (
            got[0], ACC_AT_TARGET)
    if got[1] != COUNTER:
        return "_counter reads %d, expected %d" % (got[1], COUNTER)
    return None


def bare_var_keeps_cell(out):
    """`var NAME' on a name the file placed must repeat that cell, not hand out a
    slot of the simulator's own variable store."""
    loc = var_row(out, "_counter")
    if loc is None:
        return "_counter lost its binding"
    if "variables[" in loc:
        return "`var _counter' replaced the loaded cell with %s" % loc
    return None


def bare_var_still_allocates(out):
    """...and a name nobody placed still gets a scratch cell, which is what
    bpcmdtest pins; the guard added for symbol files must not eat it."""
    return binds("scratch", "variables[")(out)


def missing_file(out):
    if not has(out, "cannot open"):
        return "a missing symbol file was accepted in silence"
    return unbound("_main")(out)


def warns_about_areas(out):
    """A `.sym' file has no area bases (only the linker's map knows them), so a
    symbol the assembler left relocatable is bound at the value in the file.  The
    load has to say that out loud rather than be quietly wrong."""
    if not has(out, "relocatable"):
        return "no warning about area-relative values"
    return None


def auto_load_pair():
    """A directory of the suite's own holding prog.ihx and prog.sym: `file
    "DIR/prog"' (the name without its extension, which is how the sibling is
    found) has to bind the names without any `sym' command.  t/ itself must never
    grow a `.sym' beside an image the other suites load."""
    d = tempfile.mkdtemp(prefix="symtest-")
    shutil.copyfile(IMG, os.path.join(d, "prog.ihx"))
    shutil.copyfile(fixture("symtest.sym"), os.path.join(d, "prog.sym"))
    return d, os.path.join(d, "prog")


def auto_loaded(out):
    """`-q' suppresses the `N symbols read' count, so what proves the sibling was
    read is the diagnostic only read_sym_file prints, plus the binding."""
    if not has(out, "kept it: ACC"):
        return "loading the image did not read the .sym beside it"
    return binds("_main", "rom[0x0030]")(out)


AUTO_DIR, AUTO_BASE = auto_load_pair()
atexit.register(shutil.rmtree, AUTO_DIR, ignore_errors=True)
# --- the legs ------------------------------------------------------------------
# (label, exact console command lines, check)

LEGS = [
    ("wide fixture binds", [F, SYM, "info variables"], wide_fixture),
    ("three-per-line binds", [F, SYMN, "info variables"], narrow_fixture),
    ("**** and bit rows refused", [F, SYM, "info variables"], refused_rows),
    ("bad line names its line", [F, SYMB, "info variables"], bad_line_refused),
    ("missing file refused", [F, 'sym "%s"' % os.path.join(T, "nope.sym"),
                              "info variables"], missing_file),
    ("area values warned about", [F, SYM, "info variables"], warns_about_areas),
    # names where addresses are accepted today
    ("break <name> fires (104)", [F, SYM, "break _load_acc", SENT,
                                  "info break", "run"], break_by_name),
    ("break <name> needs the file", [F, "break _load_acc", SENT,
                                     "info break", "run"], break_without_symbols),
    ("break <name> row at target", [F, SYM, "break _load_acc", "info break"],
     lambda out: None if bp_rows(out, TARGET) else "no row at 0x%06x" % TARGET),
    ("expression <name> == number", [F, SYM, "expression _counter",
                                     "expression iram[0x30]", "quit"],
     data_name_matches_number),
    ("sfr name not shadowed", [F, SYM, "break _load_acc", "run",
                               "expression ACC", "expression _counter",
                               "info variables", "quit"], sfr_not_shadowed),
    ("bare var keeps loaded cell", [F, SYM, "var _counter", "info variables"],
     bare_var_keeps_cell),
    ("bare var still allocates", [F, SYM, "var scratch", "info variables"],
     bare_var_still_allocates),
    ("image loads its own .sym", ['file "%s"' % AUTO_BASE, "info variables"],
     auto_loaded),
]


def main():
    if not os.path.exists(SIM):
        print("symtest: no simulator at %s" % SIM)
        return 2
    if not os.path.exists(IMG):
        print("symtest: no fixture at %s" % IMG)
        return 2
    failed = 0
    for label, cmds, check in LEGS:
        out = run(cmds)
        problem = check(out)
        if problem:
            failed += 1
            print("FAIL %-30s %s" % (label, problem))
            print("     $ %s" % " | ".join(cmds[1:]))
        else:
            print("ok   %-30s" % label)
    print("symtest: %s %d/%d legs %s" % (
        "PASS" if not failed else "FAIL", len(LEGS) - failed, len(LEGS),
        "" if not failed else "(%d failed)" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
