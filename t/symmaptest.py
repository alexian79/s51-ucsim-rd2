#!/usr/bin/env python3
"""symmaptest - one `.sym' file, two tools, one answer.

WHAT THIS PROVES
----------------
`uCsim' binds the names of an ASxxxx symbol table with cl_uc::read_sym_file() plus
cl_51core::sym_memory(); gh/LoadSym51.java does it for Ghidra.  Both read the same bytes
here - t/isa.ihx and the t/symtest*.sym fixtures of t/symtest.py - and each side is
asked what it named and where:

  * the simulator: `info variables' before and after `sym FILE'.  The names that appear
    only after the load are what the FILE placed; nothing is inferred from the fixture.
  * Ghidra: LoadSym51.java, which dumps the symbol table twice (SYMBEFORE, SYMALL).  The
    names that appear only in the second dump are what the file placed, again whatever
    the tool itself says rather than what this script expects.

The two sets are then compared name by name and address by address, in the corresponding
address space (see SPACE_OF).  Neither side's set is derived from the .sym file itself, so
a loader that silently drops a row, or parks one in a space the other tool does not use,
shows up as a missing name or a wrong space rather than passing.

THE AREA -> SPACE MAPPING, AND THE NAMES IT LANDS ON
----------------------------------------------------
The simulator's memories are rom/iram/sfr/xram; the language's spaces are CODE/INTMEM/
SFR/EXTMEM (RAM and OTHER exist too, but no `.sym' area can reach them), and leg 1 asserts
that all four are really there rather than trusting the name.  An area name says code or
data exactly as cl_51core::sym_memory decides it, and a data address then says which of
them: <=0x7f internal RAM, <=0xff the SFRs, beyond that external data.  A BSEG (bit) area
says nothing: the value is a bit number, so
there is no byte cell to name and BOTH tools refuse the row - leg 5 checks the refusal on
both sides so the agreement below cannot be reached by one tool quietly omitting it.

WHERE THE AREA NAME COMES FROM (leg 10 depends on this)
-------------------------------------------------------
A symbol record's first four columns are a hex area REFERENCE NUMBER - ` %2X ' in
lstsym(), which is why the shipped fixtures read `C _main', `B _counter', `D _bitvar' -
and that number is turned into a name only by the Area Table rows printed after the
symbols: `  %2X ' + `%-8.8s' + `   size %4X   flags %4X\n' in the default hex,
2-byte-address, non-LONGINT build (aslist.c:1268, :1278, :1319; the octal xflag branch
uses ` %3o ' and -w widens the name) - first column the same number: `0 _CODE',
`D BSEG').  There is no area-name column on a symbol row and nothing is guessed: both
tools look the string up by number.  So an external-data symbol is one whose AREA is
XSEG/XISEG (or sdcc's `_XSEG'), and those are real emitted names, not inventions of this
suite: sdld's AOMF51 area table (sdas/linksrc/lkaomf51.c) declares `{"XSEG", 1}' and
`{"XISEG", 1}' with class 1 = external RAM, and lkmem.c grows `XRam' from
`EQ(xp->a_id, "XSEG") || EQ(xp->a_id, "XISEG")'.  Leg 10 writes such a `.sym' (to TMP,
never to t/) with values 0x0100, 0x0200 and 0x1234 - one per block of external data - and
requires the simulator to answer xram and Ghidra to answer EXTMEM at the same offset.
XRAM (EXTMEM:0000-06ff) and XDATA (EXTMEM:0700-ffff) are two pspec memory BLOCKS of ONE
address space, not two spaces, so all three belong in EXTMEM.

WHY THE CONTROL AND THE MISMATCH ARE NOT OPTIONAL
-------------------------------------------------
  * leg 3: with no `.sym' on either side, neither tool names `_load_acc'.  Without it the
    agreement could be reached by nothing being loaded anywhere.
  * leg 4: a copy of the fixture with exactly one record's value altered (`_load_acc'
    003A -> 003C) is given to one tool at a time, and the gate MUST report that name with
    both addresses.  A gate that can only pass proves nothing.  `symmaptest.py --skew'
    hands the skewed run to the agreement leg itself: the whole gate then reports
    `FAIL 9/10 legs' and exits 1, which is the demonstration, never a mode that passes.

WHAT THE TWO TOOLS GENUINELY DO DIFFERENTLY (measured, reported, not asserted away)
-----------------------------------------------------------------------------------
  * wording: the simulator prints `sym: FILE:LINE: <why>, nothing after it is loaded' once
    for a refused row; LoadSym51 prints `bad_line=LINE bad_why=<why>' on its summary line.
    Same line, same rows dropped; leg 9 pins the behaviour and prints both strings.
  * primary-ness: `_sp' at 0x81 already has the pspec's `SP' there, so Ghidra's symbol is
    secondary (primary=false) while the simulator has no such distinction.  The cell is the
    same; only the flag differs, and leg 2 prints it rather than requiring it.
  * a name the language already binds (ACC, IE) keeps its binding on both sides - the
    simulator's precedence rule (VBY_PRE beats VBY_DEBUG) and LoadSym51's `already_bound'
    skip are the same decision, pinned by leg 7.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

S51 = os.environ.get("S51") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.environ.get("S51_SIM") or os.path.join(
    S51, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
GHIDRA = os.path.expanduser(os.environ.get("GHIDRA", "~/ghidra_12.1.4_PUBLIC"))
HEADLESS = os.path.join(GHIDRA, "support", "analyzeHeadless")
RD2 = "AT89C51RD2:BE:16:default"
SCRIPT = "LoadSym51.java"
TMP = "/tmp/symmaptest"
T = os.path.join(S51, "t")
IMG = os.path.join(T, "isa.ihx")

#: simulator memory -> the address space this language gives the same storage
SPACE_OF = {"rom": "CODE", "iram": "INTMEM", "sfr": "SFR", "xram": "EXTMEM"}


def fixture(name):
    path = os.path.join(T, name)
    if not os.path.exists(path):
        print("symmaptest: no fixture at %s" % path)
        sys.exit(2)
    return path


WIDE = fixture("symtest.sym")
NARROW = fixture("symtest_narrow.sym")
BAD = fixture("symtest_bad.sym")


def skewed_fixture():
    """A copy of the wide fixture with exactly one record's value altered: `_load_acc'
    moves from 003A to 003C, one token on one line, nothing else touched.  Leg 4 hands
    this to one tool only and requires the gate to fail."""
    out = os.path.join(TMP, "skew.sym")
    lines = open(WIDE).read().splitlines(True)
    hit = 0
    for i, line in enumerate(lines):
        if "_load_acc" in line and "003A" in line:
            lines[i] = line.replace("003A", "003C")
            hit += 1
    if hit != 1:
        print("symmaptest: could not skew %s (touched %d lines)" % (WIDE, hit))
        sys.exit(2)
    with open(out, "w") as f:
        f.write("".join(lines))
    return out


def xseg_fixture():
    """A `.sym' whose records sit in an external-data area, i.e. values past 0xff - the
    one branch of cl_51core::sym_memory() no shipped fixture reaches.

    WHERE THE AREA STRING COMES FROM.  A symbol record's first four columns are a hex
    area REFERENCE NUMBER (` %2X ' in lstsym(); that is why the shipped fixtures read
    `C _main', `B _counter', `D _bitvar'), and the number is turned into a name only by
    the Area Table rows printed after the symbols (`  %2X %-8.8s   size %4X   flags %4X',
    whose first column is that same number: `0 _CODE', `D BSEG').  So the string a tool
    matches against DATA_AREAS is looked up by number, never read off the symbol row and
    never guessed - there is no fourth area-name column in the format.

    XSEG IS NOT INVENTED FOR THIS LEG.  sdld's AOMF51 area table (sdas/linksrc/
    lkaomf51.c) declares `{"XSEG", 1}' and `{"XISEG", 1}' with class 1 = external RAM,
    and lkmem.c grows `XRam' from `EQ(xp->a_id, "XSEG") || EQ(xp->a_id, "XISEG")';
    sdcc's mcs51 back end spells the same area `_XSEG', which is what the leading-
    underscore strip in sym_memory() exists for.  Both spellings are exercised.

    It goes to TMP: t/ must never grow a `.sym' beside an image another suite loads.
    """
    def row(ref, name, value, flags):
        return "  %s %s %04X %s\n" % (ref, name.ljust(60), value, flags)

    def area(ref, name, size, flags):
        return "  %s %s   size %4X   flags %4X\n" % (ref, name.ljust(60), size, flags)

    out = os.path.join(TMP, "xseg.sym")
    with open(out, "w") as f:
        f.write("ASxxxx Assembler V01.80 + NoICE + SDCC mods  (Intel 8051), page 1.\n\n"
                "Symbol Table\n\n")
        f.write(row("E", "_xd_far", 0x1234, "GR"))     # XDATA block of EXTMEM
        f.write(row("E", "_xd_near", 0x0100, "GR"))    # XRAM block of EXTMEM
        f.write(row("F", "_xd_uscore", 0x0200, "GR"))  # sdcc's `_XSEG' spelling
        f.write("\nArea Table\n\n")
        f.write(area("E", "XSEG", 0x40, 0))
        f.write(area("F", "_XSEG", 0x40, 0))
    return out


# --- the simulator side ---------------------------------------------------------------

def sim(cmds, timeout=25):
    """Drive the console the way symtest.py does: hold stdin open past the last command,
    because a session killed at a timeout loses the output that proves the leg."""
    quoted = " ".join(shlex.quote(c) for c in cmds)
    pipe = '( printf "%%s\\n" %s; sleep 1 ) | %s -t 89C51R -q -c -' % (
        quoted, shlex.quote(SIM))
    try:
        p = subprocess.run(["sh", "-c", pipe], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<<timeout>>"
    return p.stdout + p.stderr


def sim_vars(out):
    """name -> (memory, address) from an `info variables' table.  The binding column is
    what says where a name lives: `rom[0x003a]', `iram[0x30]', `sfr[0xe0][7:0]'."""
    got = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+) (\w+)\[0x([0-9a-f]+)\]", line.strip())
        if m:
            got[m.group(1)] = (m.group(2), int(m.group(3), 16))
    return got


def sim_run(sym):
    cmds = ['file "%s"' % IMG]
    if sym:
        cmds.append('sym "%s"' % sym)
    cmds += ["info variables", "quit"]
    return sim(cmds)


# --- the Ghidra side ------------------------------------------------------------------

def ghidra(tag, sym=None):
    """Import the image under the RD2 language with no analysis and run LoadSym51 on it,
    passing the .sym path as the script's one argument.  One analyzeHeadless at a time,
    and one project for the whole run set: each import goes in under its own name."""
    src = os.path.join(TMP, "in")
    os.makedirs(src, exist_ok=True)
    img = os.path.join(src, "%s.ihx" % tag)
    shutil.copyfile(IMG, img)
    cmd = [HEADLESS, os.path.join(TMP, "proj"), "p", "-import", img,
           "-processor", RD2, "-noanalysis",
           "-scriptPath", os.path.join(S51, "gh"), "-postScript", SCRIPT]
    if sym:
        cmd.append(sym)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    out = p.stdout + p.stderr
    mark = "%s> " % SCRIPT          # langtest.py's convention, including the suffix strip
    return "\n".join(re.sub(r" \(GhidraScript\)\s*$", "", l.split(mark, 1)[1])
                     for l in out.splitlines() if mark in l)


class Run(object):
    """One LoadSym51 session, as the tool reported it."""

    def __init__(self, out):
        self.out = out
        self.before = {}
        self.after = {}
        self.placed = {}
        self.skips = {}
        self.load = ""
        self.spaces = []
        self.rows = {"SYMBEFORE": 0, "SYMALL": 0}
        for line in out.splitlines():
            m = re.match(r"^(SYMBEFORE|SYMALL) (\S+):([0-9a-f]+) (\S+) primary=(\w+)", line)
            if m:
                table = self.before if m.group(1) == "SYMBEFORE" else self.after
                self.rows[m.group(1)] += 1
                table[m.group(4)] = (m.group(2), int(m.group(3), 16), m.group(5) == "true")
                continue
            m = re.match(r"^SYM (\S+):([0-9a-f]+) (\S+) primary=(\w+)", line)
            if m:
                self.placed[m.group(3)] = (m.group(1), int(m.group(2), 16),
                                           m.group(4) == "true")
                continue
            if line.startswith("SKIP "):
                self.skips[line.split()[1]] = line
            elif line.startswith("LOAD "):
                self.load = line
            elif line.startswith("SPACE "):
                self.spaces.append(line.split()[1])

    @property
    def added(self):
        """What the file placed, as the symbol table itself shows it."""
        return dict((n, v) for n, v in self.after.items() if n not in self.before)

    @property
    def changed(self):
        """Names the load moved - empty for a correct loader, and checked to be."""
        return dict((n, (self.before[n], v)) for n, v in self.after.items()
                    if n in self.before and self.before[n][:2] != v[:2])

    def cells(self):
        return dict((n, (v[0], v[1])) for n, v in self.added.items())

    def skip_reason(self, name):
        m = re.search(r"reason=(\S+)", self.skips.get(name, ""))
        return m.group(1) if m else None

    def load_field(self, key):
        m = re.search(r"\b%s=(\S+)" % key, self.load)
        return m.group(1) if m else None


# --- the agreement check --------------------------------------------------------------

def agree(sim_added, gh_added):
    """The cross-tool mismatch report; [] means both tools named the same things at the
    same cells of the corresponding spaces.  Every line names the symbol and both sides."""
    problems = []
    for name in sorted(set(sim_added) | set(gh_added)):
        s = sim_added.get(name)
        g = gh_added.get(name)
        if s is None:
            problems.append("%s: ghidra %s:0x%x, simulator placed it nowhere"
                            % (name, g[0], g[1]))
            continue
        if g is None:
            problems.append("%s: simulator %s[0x%x], ghidra placed it nowhere"
                            % (name, s[0], s[1]))
            continue
        want = SPACE_OF.get(s[0])
        if want is None:
            problems.append("%s: no space mapping for the simulator memory `%s'"
                            % (name, s[0]))
            continue
        if want != g[0] or s[1] != g[1]:
            problems.append("%s: simulator %s[0x%x] != ghidra %s:0x%x"
                            % (name, s[0], s[1], g[0], g[1]))
    return problems


def describe(cells):
    return ", ".join("%s=%s:0x%x" % (n, v[0], v[1]) for n, v in sorted(cells.items())) or "-"


def fmt(cells):
    return " ".join("%s@%s[0x%x]" % (n, v[0], v[1]) for n, v in sorted(cells.items()))


# --- the legs -------------------------------------------------------------------------

def check_spaces(c):
    spaces = c["gh_base"].spaces
    missing = [s for s in SPACE_OF.values() if s not in spaces]
    if missing:
        return "language has no address space named %s (has %s)" % (
            ", ".join(missing), " ".join(spaces))
    print("     spaces: %s" % " ".join(spaces))
    print("     map: %s" % " ".join("%s->%s" % kv for kv in sorted(SPACE_OF.items())))
    return None


def check_agree(c):
    problems = agree(c["sim_wide"], c["gh_wide"].cells())
    for p in problems:
        print("     %s" % p)
    if problems:
        return "simulator and ghidra disagree on %d name(s)" % len(problems)
    if c["gh_wide"].changed:
        return "ghidra moved a symbol it did not place: %s" % (c["gh_wide"].changed,)
    if len(c["sim_wide"]) < 4:
        return "the simulator placed only %d name(s): %s" % (
            len(c["sim_wide"]), fmt(c["sim_wide"]))
    spaces = set(v[0] for v in c["sim_wide"].values())
    gspaces = set(v[0] for v in c["gh_wide"].cells().values())
    if len(spaces) < 3 or len(gspaces) < 3:
        return "the placement used %s / %s, not the code+data+sfr spread the fixture has" % (
            sorted(spaces), sorted(gspaces))
    print("     %d names agree: sim %s" % (len(c["sim_wide"]), fmt(c["sim_wide"])))
    print("     ghidra %s" % describe(c["gh_wide"].cells()))
    secondary = sorted(n for n, v in c["gh_wide"].added.items() if not v[2])
    print("     ghidra secondary at an address the pspec already named: %s"
          % (", ".join(secondary) or "none"))
    return None


def check_control(c):
    for name in ("_load_acc", "_main", "_counter"):
        if name in c["sim_base_vars"]:
            return "the simulator names %s at %s with no symbol file" % (
                name, c["sim_base_vars"][name])
        if name in c["gh_base"].after:
            return "ghidra names %s at %s with no symbol file" % (
                name, c["gh_base"].after[name])
    if c["gh_base"].added or c["gh_base"].placed:
        return "ghidra placed %s with no .sym given" % describe(c["gh_base"].added)
    print("     %d simulator bindings, %d ghidra symbols, none of them the fixture's names"
          % (len(c["sim_base_vars"]), c["gh_base"].rows["SYMALL"]))
    return None


def check_mismatch(c):
    """The injected error: one record's value altered, given to one tool at a time."""
    want = ("_load_acc", ("rom", 0x3a), ("CODE", 0x3c))
    problems = agree(c["sim_wide"], c["gh_skew"].cells())
    one = [p for p in problems if want[0] in p]
    if len(problems) != 1 or not one:
        return "ghidra skewed: expected one report naming _load_acc, got %s" % (problems,)
    if "0x3a" not in one[0] or "0x3c" not in one[0]:
        return "ghidra skewed: the report does not carry both addresses: %s" % one[0]
    print("     ghidra skewed: %s" % one[0])
    problems = agree(c["sim_skew"], c["gh_pristine"].cells())
    one = [p for p in problems if want[0] in p]
    if len(problems) != 1 or not one:
        return "simulator skewed: expected one report naming _load_acc, got %s" % (problems,)
    if "0x3a" not in one[0] or "0x3c" not in one[0]:
        return "simulator skewed: the report does not carry both addresses: %s" % one[0]
    print("     simulator skewed: %s" % one[0])
    return None


def check_bit(c):
    if "_bitvar" in c["sim_wide"] or "_bitvar" in c["gh_wide"].cells():
        return "a bit symbol was parked on a byte: sim %s ghidra %s" % (
            c["sim_wide"].get("_bitvar"), c["gh_wide"].cells().get("_bitvar"))
    if "no cell here: _bitvar" not in c["out_sim_wide"]:
        return "the simulator skipped the bit row without naming it"
    if c["gh_wide"].skip_reason("_bitvar") != "no_byte_cell":
        return "ghidra's skip for _bitvar: %s" % c["gh_wide"].skips.get("_bitvar", "no line")
    if "area=BSEG" not in c["gh_wide"].skips["_bitvar"]:
        return "ghidra refused _bitvar but not for being in BSEG: %s" % (
            c["gh_wide"].skips["_bitvar"])
    print("     both refuse: sim `no cell here: _bitvar', ghidra `%s'"
          % c["gh_wide"].skips["_bitvar"])
    return None


def check_undefined(c):
    if "__undefined_sym" in c["sim_wide"] or "__undefined_sym" in c["gh_wide"].cells():
        return "a `****' row got an address anyway"
    if "have no address" not in c["out_sim_wide"]:
        return "the simulator skipped the undefined row without saying so"
    if c["gh_wide"].skip_reason("__undefined_sym") != "undefined":
        return "ghidra's skip for __undefined_sym: %s" % (
            c["gh_wide"].skips.get("__undefined_sym", "no line"))
    return None


def check_precedence(c):
    if "kept it: ACC, IE" not in c["out_sim_wide"]:
        return "the simulator did not report keeping ACC and IE: %r" % [
            l for l in c["out_sim_wide"].splitlines() if "kept" in l]
    kept = sorted(n for n, line in c["gh_wide"].skips.items()
                  if "reason=already_bound" in line)
    if kept != ["ACC", "IE"]:
        return "ghidra kept %s, expected ACC and IE" % kept
    if c["sim_wide_after"].get("ACC") != ("sfr", 0xE0):
        return "the simulator moved ACC to %s" % (c["sim_wide_after"].get("ACC"),)
    if c["gh_wide"].after.get("ACC", ("", None))[0:2] != ("SFR", 0xE0):
        return "ghidra moved ACC to %s" % (c["gh_wide"].after.get("ACC"),)
    print("     both leave ACC at sfr[0xe0]/SFR:0xe0 and report it")
    return None


def check_narrow(c):
    problems = agree(c["sim_narrow"], c["gh_narrow"].cells())
    for p in problems:
        print("     %s" % p)
    if problems:
        return "three-per-line fixture disagrees on %d name(s)" % len(problems)
    if len(c["sim_narrow"]) < 3:
        return "the simulator placed only %s" % fmt(c["sim_narrow"])
    if "_unused" in c["sim_narrow"] or "_unused" in c["gh_narrow"].cells():
        return "`****' in the narrow fixture was placed anyway"
    print("     %d names agree: %s" % (len(c["sim_narrow"]), fmt(c["sim_narrow"])))
    return None

def check_xseg(c):
    """The branch the shipped fixtures never reach: a value past 0xff in an external-data
    area.  The simulator must call it xram and Ghidra must place it in EXTMEM at the same
    offset; if either parks it in code or refuses it, this leg fails.

    The pspec splits external data into two memory BLOCKS, XRAM (EXTMEM:0000-06ff) and
    XDATA (EXTMEM:0700-ffff), but they are ONE address space: 0x0100 and 0x1234 are both
    EXTMEM, so the leg puts one symbol in each block instead of treating the block
    boundary as a space boundary.
    """
    problems = agree(c["sim_xseg"], c["gh_xseg"].cells())
    for p in problems:
        print("     %s" % p)
    if problems:
        return "external data beyond 0xff disagrees on %d name(s)" % len(problems)
    if len(c["sim_xseg"]) < 3:
        return "expected three external-data names, the simulator placed %s" % (
            fmt(c["sim_xseg"]))
    wrong = ["%s: simulator %s[0x%x]" % (n, v[0], v[1])
             for n, v in sorted(c["sim_xseg"].items()) if v[0] != "xram"]
    wrong += ["%s: ghidra %s:0x%x" % (n, v[0], v[1])
              for n, v in sorted(c["gh_xseg"].cells().items()) if v[0] != "EXTMEM"]
    if wrong:
        return "not external data on both sides: %s" % "; ".join(wrong)
    for line in c["out_sim_xseg"].splitlines():
        if re.match(r"^_xd_\S+ xram\[0x", line.strip()):
            print("     simulator: %s" % line.strip())
    for line in c["gh_xseg"].out.splitlines():
        if line.startswith("SYM ") and " _xd_" in line:
            print("     ghidra:    %s" % line)
    print("     area name looked up by number in the Area Table; `XSEG'/`_XSEG' are "
          "sdld's external-RAM areas (lkaomf51.c, lkmem.c)")
    for b in c["gh_xseg"].out.splitlines():
        if b.startswith("BLOCK") and "EXTMEM" in b:
            print("     %s" % b)
    return None


def check_bad(c):
    """Both tools have to stop at the row the assembler never printed, and say which."""
    problems = agree(c["sim_bad"], c["gh_bad"].cells())
    for p in problems:
        print("     %s" % p)
    if problems:
        return "past a malformed line the tools disagree: %s" % problems
    if "_before_bad" not in c["sim_bad"] or "_before_bad" not in c["gh_bad"].cells():
        return "the row before the bad one was not bound by both: sim %s ghidra %s" % (
            c["sim_bad"].get("_before_bad"), c["gh_bad"].cells().get("_before_bad"))
    for name in ("_after_bad",):
        if name in c["sim_bad"] or name in c["gh_bad"].cells():
            return "%s is after the refused row yet %s bound it" % (
                name, "simulator" if name in c["sim_bad"] else "ghidra")
    sim_says = [l for l in c["out_sim_bad"].splitlines() if "nothing after it" in l]
    if not any(re.search(r"symtest_bad\.sym:6\b", l) for l in sim_says):
        return "the simulator did not name line 6: %s" % (sim_says or "no such message",)
    if c["gh_bad"].load_field("bad_line") != "6":
        return "ghidra's bad_line is %s, expected 6 (%s)" % (
            c["gh_bad"].load_field("bad_line"), c["gh_bad"].load)
    print("     both stop at line 6 and bind only %s" % fmt(c["sim_bad"]))
    print("     simulator: %s" % sim_says[0].strip())
    print("     ghidra:    %s" % c["gh_bad"].load)
    return None


LEGS = [
    ("spaces exist and are mapped", check_spaces),
    ("wide fixture agrees", check_agree),
    ("control: no file, no names", check_control),
    ("altered value fails the gate", check_mismatch),
    ("bit row refused by both", check_bit),
    ("**** row refused by both", check_undefined),
    ("pspec names kept by both", check_precedence),
    ("narrow fixture agrees", check_narrow),
    ("external data beyond 0xff", check_xseg),
    ("malformed line: both stop", check_bad),
]


def main():
    for what, path in (("simulator", SIM), ("image", IMG), ("ghidra", HEADLESS),
                       ("script", os.path.join(S51, "gh", SCRIPT))):
        if not os.path.exists(path):
            print("symmaptest: no %s at %s" % (what, path))
            return 2
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(os.path.join(TMP, "proj"))
    skew = skewed_fixture()
    XSEG = xseg_fixture()

    c = {}
    c["out_sim_base"] = sim_run(None)
    c["out_sim_wide"] = sim_run(WIDE)
    c["out_sim_narrow"] = sim_run(NARROW)
    c["out_sim_bad"] = sim_run(BAD)
    c["out_sim_skew"] = sim_run(skew)
    c["out_sim_xseg"] = sim_run(XSEG)
    c["gh_base"] = Run(ghidra("base"))
    c["gh_wide"] = Run(ghidra("wide", WIDE))
    c["gh_narrow"] = Run(ghidra("narrow", NARROW))
    c["gh_bad"] = Run(ghidra("bad", BAD))
    c["gh_skew"] = Run(ghidra("skew", skew))
    c["gh_xseg"] = Run(ghidra("xseg", XSEG))

    if not c["gh_base"].spaces:
        print("symmaptest: %s produced nothing at all, nothing was measured" % SCRIPT)
        print(c["gh_base"].out[-2000:])
        return 2
    c["sim_base_vars"] = sim_vars(c["out_sim_base"])
    missing = [n for n in ("ACC", "IE", "SP", "AUXR1") if n not in c["sim_base_vars"]]
    if missing or len(c["sim_base_vars"]) < 150:
        print("symmaptest: `info variables' gave %d rows and no %s, the model did not load"
              % (len(c["sim_base_vars"]), ", ".join(missing) or "name"))
        return 2
    if len(c["gh_base"].after) < 300:
        print("symmaptest: ghidra gave %d symbols, the language did not load"
              % len(c["gh_base"].after))
        return 2

    wide_after = sim_vars(c["out_sim_wide"])
    skew_after = sim_vars(c["out_sim_skew"])
    c["sim_wide_after"] = wide_after
    c["sim_wide"] = dict((n, v) for n, v in wide_after.items()
                         if n not in c["sim_base_vars"])
    c["sim_narrow"] = sim_delta(sim_vars(c["out_sim_narrow"]), c["sim_base_vars"])
    c["sim_bad"] = sim_delta(sim_vars(c["out_sim_bad"]), c["sim_base_vars"])
    c["sim_xseg"] = sim_delta(sim_vars(c["out_sim_xseg"]), c["sim_base_vars"])
    c["sim_skew"] = dict((n, v) for n, v in skew_after.items()
                         if n not in c["sim_base_vars"])

    c["gh_pristine"] = c["gh_wide"]
    if "--skew" in sys.argv:
        print("symmaptest: INJECTED MISMATCH - the agreement leg gets ghidra's skewed")
        print("              run, so a gate that could not fail would prove nothing")
        c["gh_wide"] = c["gh_skew"]

    failed = 0
    for label, check in LEGS:
        problem = check(c)
        if problem:
            failed += 1
            print("FAIL %-30s %s" % (label, problem))
        else:
            print("ok   %-30s" % label)
    print("symmaptest: %s %d/%d legs %s" % (
        "PASS" if not failed else "FAIL", len(LEGS) - failed, len(LEGS),
        "" if not failed else "(%d failed)" % failed))
    return 1 if failed else 0


def sim_delta(after, before):
    return dict((n, v) for n, v in after.items() if n not in before)


if __name__ == "__main__":
    sys.exit(main())
