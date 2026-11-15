#!/usr/bin/env python3
"""Pin what the console's `break' and `var' commands accept, and what they refuse.

Both commands pick a branch out of a syntax chain, and each branch reads fixed
parameter slots.  Before the fix in cmd_bp.cc a branch could match a command
legitimately and then throw part of it away in silence:

  * `break 0x3a if 0' matched CELL STRING STRING (a bare number is a cell, and a
    number argument carries no string value), so the condition became the empty
    string and the command created an UNCONDITIONAL breakpoint;
  * `break 0x3a iff ACC==0x5a' matched the same branch and the keyword test
    `if (s == "if")' fell through, again silently unconditional;
  * `break 0x3a hit 2' was discarded the same way and kept Hit 1.

A debugger that stops somewhere you did not ask for is worse than one that
refuses, so those forms are refused now and `hit' is honoured.  A refused form
must create NO breakpoint at all, and the forms that already worked must keep
working at both polarities, so that a future change to the chain flips something
visible.

Two output traps that made an earlier investigation of this area wrong are built
into the checks:

  * count the stop CODE, never `Stop at <addr>': (104) is a breakpoint firing,
    (109) is a `step' landing on that address, (112) an event breakpoint;
  * the table's `Cond' column reads false on every conditional row whether or not
    the condition held.  The condition text appears only in the trailing
    cond="...", and it is never what decides a stop.
"""
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simproc import run_pipe

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S51 = os.environ.get("S51") or HERE
SIM = os.environ.get("S51_SIM") or os.path.join(
    S51, "vendor/ucsim-stc/ucsim/src/sims/s51.src/ucsim_51")
IMG = os.path.join(HERE, "t", "isa.ihx")   # fixture: beside the suite, not under $S51
F = 'file "%s"' % IMG

# t/isa.ihx runs 0x33 `MOV A,#0x5a' and leaves 0x5a at iram[0x40] as well, so one
# constant drives both the accumulator condition and the RAM-byte condition, and
# the wrong-polarity constants (0x5b, 0x00) give the matching negatives.

# A session whose breakpoint never fires would simulate forever, and a simulator
# killed at a timeout loses its buffered output -- which reads exactly like "no
# breakpoint was created".  So sessions that must terminate carry a sentinel
# breakpoint past the target: 0x44 is on this image's path after 0x3a, and its
# stop is never the thing under test.
SENT = "break 0x44"
TARGET = 0x3a
CELL = 0x30                    # the iram byte the watchpoint legs arm on


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
    """Stops with `code' at `addr'.  The address belongs in the count: a session
    also carries a sentinel breakpoint, and `Stop at <addr>' is printed for every
    stop reason -- (104) fired, (109) a step landing, (112) an event break."""
    return len(re.findall(r'Stop at 0x%06x: \(%d\)' % (addr, code), out))


def var_row(out, name):
    """Where `info variables' says a name is bound, or None."""
    for line in out.splitlines():
        m = re.match(r'^%s\s+(\S+)\s*=' % re.escape(name), line.strip())
        if m:
            return m.group(1)
    return None


# --- the checks ----------------------------------------------------------------

def refused(out):
    """Nothing at the target address in the table, and nothing stops there."""
    rows = bp_rows(out, TARGET)
    if rows:
        return "created %s" % rows[0]["text"]
    if fired(out):
        return "stopped at the target although no breakpoint is armed there"
    return None


def cond_fires(expect):
    def check(out):
        if not bp_rows(out, TARGET):
            return "no breakpoint was created at the target"
        got = fired(out)
        if expect and got != 1:
            return "expected one (104) at the target, got %d" % got
        if not expect and got:
            return "expected no (104) at the target, got %d" % got
        return None
    return check


def row_wants(hits=None, typ=None, addr=TARGET):
    def check(out):
        rows = bp_rows(out, addr)
        if not rows:
            return "no breakpoint was created"
        r = rows[0]
        if typ and r["type"] != typ:
            return "type is %s, expected %s" % (r["type"], typ)
        if hits is not None and r["hit"] != hits:
            return "Hit is %d, expected %d" % (r["hit"], hits)
        return None
    return check


def second_pass(out):
    """Hit 2 means the second crossing, and isa.ihx crosses 0x3a once."""
    if not bp_rows(out, TARGET):
        return "no breakpoint was created"
    if fired(out):
        return "fired on the first crossing"
    return None


def says_why(out):
    if "A condition has to be" not in out:
        return "refused without saying that a number is not a condition"
    return refused(out)


def binds(name, want):
    def check(out):
        loc = var_row(out, name)
        if loc is None:
            return "no variable %s was created" % name
        if want not in loc:
            return "%s bound to %s, expected %s" % (name, loc, want)
        return None
    return check


def unbound(name):
    def check(out):
        loc = var_row(out, name)
        if loc is not None:
            return "`%s' created a variable bound to %s" % (name, loc)
        return None
    return check


# --- the legs ------------------------------------------------------------------
# (label, exact console command lines, check)

BROKEN = [
    ("numeric condition refused", "if 0"),
    ("numeric 1 condition refused", "if 1"),
    ("misspelled keyword refused", "iff ACC==0x5a"),
    ("if without operand refused", "if"),
    ("junk after condition refused", "if ACC==0x5b junk"),
    ("unquoted brackets refused", "if iram[0x40]==0x5a"),
]
LEGS = [(label, [F, "break 0x3a " + tail, SENT, "info break", "run"], refused)
        for label, tail in BROKEN]

LEGS += [
    ("refusal explains itself", [F, "break 0x3a if 0", "info break"], says_why),
    ("cond ACC==true fires",
     [F, "break 0x3a if ACC==0x5a", SENT, "info break", "run"], cond_fires(True)),
    ("cond ACC==false misses",
     [F, "break 0x3a if ACC==0x5b", SENT, "info break", "run"], cond_fires(False)),
    ("quoted cond true fires",
     [F, 'break 0x3a if "ACC == 0x5a"', SENT, "info break", "run"], cond_fires(True)),
    ("quoted cond false misses",
     [F, 'break 0x3a if "ACC == 0x00"', SENT, "info break", "run"], cond_fires(False)),
    ("quoted brackets true fires",
     [F, 'break 0x3a if "iram[0x40]==0x5a"', SENT, "info break", "run"],
     cond_fires(True)),
    ("quoted brackets false misses",
     [F, 'break 0x3a if "iram[0x40]==0x5b"', SENT, "info break", "run"],
     cond_fires(False)),
    ("var cond true fires",
     [F, "var z iram[0x40]", "break 0x3a if z==0x5a", SENT, "info break", "run"],
     cond_fires(True)),
    ("var cond false misses",
     [F, "var z iram[0x40]", "break 0x3a if z==0x5b", SENT, "info break", "run"],
     cond_fires(False)),
    ("positional hit keeps 2", [F, "break 0x3a 2", "info break"], row_wants(hits=2)),
    ("keyword hit honoured", [F, "break 0x3a hit 2", "info break"], row_wants(hits=2)),
    ("hit 2 waits for second pass",
     [F, "break 0x3a 2", SENT, "info break", "run"], second_pass),
    # an event breakpoint's row carries the MEMORY address, not the code address
    ("watchpoint default hit 1", [F, "break iram w 0x30", "info break"],
     row_wants(hits=1, typ="event", addr=CELL)),
    ("watchpoint keyword hit", [F, "break iram w 0x30 hit 2", "info break"],
     row_wants(hits=2, typ="event", addr=CELL)),
    ("var mem addr binds", ["var counter iram 0x30", "info variables"],
     binds("counter", "iram[0x30]")),
    ("var mem[addr] binds", ["var flag iram[0x40]", "info variables"],
     binds("flag", "iram[0x40]")),
    ("var sfr alias binds", ["var a sfr[0xe0]", "info variables"], binds("a", "sfr[0xe0]")),
    ("bare var gets its own slot", ["var scratch", "info variables"],
     binds("scratch", "variables[")),
    # the usage line used to read `var name [memory addr]', which people type
    # literally; it has to be refused rather than quietly naming the engine's own
    # variable store instead of the firmware byte
    ("literal `memory' refused", ["var counter memory 0x30", "info variables"],
     unbound("counter")),
]


def main():
    if not os.path.exists(SIM):
        print("bpcmdtest: no simulator at %s" % SIM)
        return 2
    if not os.path.exists(IMG):
        print("bpcmdtest: no fixture at %s" % IMG)
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
    # unambiguous on purpose: a `23/25' that counts passes reads as a disaster
    print("bpcmdtest: %s %d/%d legs %s" % (
        "PASS" if not failed else "FAIL", len(LEGS) - failed, len(LEGS),
        "" if not failed else "(%d failed)" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
